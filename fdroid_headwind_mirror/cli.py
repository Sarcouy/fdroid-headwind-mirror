from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from typing import Annotated, NoReturn

import structlog
import typer
from pydantic import ValidationError

from fdroid_headwind_mirror.config import (
    ConfigError,
    PackagesFile,
    ResolvedPackage,
    Settings,
    load_packages_file,
)
from fdroid_headwind_mirror.domain.planner import (
    PackagePlan,
    PlanStatus,
    SignerState,
    SyncPlan,
    build_plan,
)
from fdroid_headwind_mirror.domain.linker import LinkingOutcome, LinkingSummary, link_plan
from fdroid_headwind_mirror.domain.publisher import (
    PublicationOutcome,
    PublicationSummary,
    publish_plan,
)
from fdroid_headwind_mirror.domain.reconciler import (
    PackageReport,
    PackageStatus,
    ReconciliationReport,
    reconcile,
)
from fdroid_headwind_mirror.domain.verifier import VerificationSummary, verify_plan
from fdroid_headwind_mirror.fdroid.cache import IndexCache, IndexRefresh, refresh_index
from fdroid_headwind_mirror.fdroid.client import FDroidClient, FDroidError
from fdroid_headwind_mirror.fdroid.download import ApkStore
from fdroid_headwind_mirror.headwind.client import HeadwindClient
from fdroid_headwind_mirror.headwind.errors import (
    HeadwindCredentialsError,
    HeadwindError,
    HeadwindPermissionError,
)
from fdroid_headwind_mirror.reporting.report import RunReport, build_report
from fdroid_headwind_mirror.state.repository import StateRepository, SyncRun

app = typer.Typer(
    help="Synchronise les mises a jour F-Droid vers Headwind MDM.", no_args_is_help=True
)

_STATUS_LABEL: dict[PackageStatus, str] = {
    PackageStatus.RESOLVED: "OK",
    PackageStatus.PAUSED: "EN PAUSE",
    PackageStatus.NOT_IN_HEADWIND: "ABSENT DE HEADWIND",
    PackageStatus.AMBIGUOUS: "AMBIGU",
}

_PLAN_LABEL: dict[PlanStatus, str] = {
    PlanStatus.UPDATE_AVAILABLE: "MISE A JOUR",
    PlanStatus.UP_TO_DATE: "A JOUR",
    PlanStatus.REJECTED: "REFUS",
    PlanStatus.NOT_IN_FDROID: "ABSENT DE F-DROID",
    PlanStatus.SKIPPED: "IGNORE",
}

_PERMISSION_MESSAGE = (
    "Acces refuse par Headwind: l'utilisateur de service doit avoir le role Utilisateur."
)

_CREDENTIALS_MESSAGE = (
    "Authentification refusee par Headwind: verifier FHM_HEADWIND_LOGIN et FHM_HEADWIND_PASSWORD."
)


@app.callback()
def main() -> None:
    """Point d'entree du service."""
    _configure_logging()


def _configure_logging() -> None:
    # Le journal part sur stderr: --json ecrit son rapport sur stdout, et les deux flux doivent
    # rester analysables separement quand le service tourne sous timer.
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )


@app.command()
def status(
    as_json: Annotated[bool, typer.Option("--json", help="Sortie JSON.")] = False,
    show_untracked: Annotated[
        bool, typer.Option("--show-untracked", help="Lister les applications Headwind non suivies.")
    ] = False,
) -> None:
    """Confronte packages.yaml aux applications declarees dans Headwind."""
    settings = _load_settings()
    packages = _load_packages(settings)

    with StateRepository(settings.database_path) as repository:
        run_id = repository.start_run()
        try:
            with _headwind_client(settings) as client:
                applications = client.list_applications()
        except HeadwindCredentialsError as exc:
            _fail(repository, run_id, "headwind.credentials_refused", exc, _CREDENTIALS_MESSAGE)
        except HeadwindPermissionError as exc:
            _fail(repository, run_id, "headwind.permission_denied", exc, _PERMISSION_MESSAGE)
        except HeadwindError as exc:
            _fail(repository, run_id, "headwind.unreachable", exc, f"Headwind injoignable: {exc}")

        report = reconcile(packages.resolved(), applications, repository, run_id)
        repository.finish_run(
            run_id,
            "OK" if report.blocking_count == 0 else "WARNING",
            packages_checked=len(report.packages),
            errors=report.blocking_count,
        )

    if as_json:
        typer.echo(json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False))
    else:
        _render(report, len(applications), show_untracked)

    raise typer.Exit(code=1 if report.blocking_count else 0)


@app.command()
def sync(
    dry_run: Annotated[
        bool, typer.Option("--dry-run/--apply", help="N'ecrit rien dans Headwind.")
    ] = True,
    as_json: Annotated[bool, typer.Option("--json", help="Sortie JSON.")] = False,
    verify_apk: Annotated[
        bool,
        typer.Option(
            "--verify-apk",
            help="Telecharge les APK a publier et verifie leur empreinte sha256"
            " (implicite avec --apply).",
        ),
    ] = False,
) -> None:
    """Confronte les versions F-Droid aux versions publiees dans Headwind."""
    settings = _load_settings()
    packages = _load_packages(settings)
    declared = packages.resolved()

    with StateRepository(settings.database_path) as repository:
        run_id = repository.start_run()
        try:
            plan = _build_sync_plan(settings, packages, declared, repository, run_id)
            # Publier une URL sans avoir verifie les octets qu'elle sert reviendrait a faire
            # confiance au depot sur parole: --apply impose donc la verification.
            verification = (
                _verify_apks(settings, packages, plan, repository, run_id)
                if verify_apk or not dry_run
                else None
            )
            publication, linking = (
                (None, None) if dry_run else _apply(settings, plan, repository, run_id)
            )
        except HeadwindCredentialsError as exc:
            _fail(repository, run_id, "headwind.credentials_refused", exc, _CREDENTIALS_MESSAGE)
        except HeadwindPermissionError as exc:
            _fail(repository, run_id, "headwind.permission_denied", exc, _PERMISSION_MESSAGE)
        except HeadwindError as exc:
            _fail(repository, run_id, "headwind.unreachable", exc, f"Headwind injoignable: {exc}")
        except FDroidError as exc:
            _fail(repository, run_id, "fdroid.unavailable", exc, f"Depot F-Droid: {exc}")

        errors = (
            plan.rejections
            + (publication.failed if publication else 0)
            + (linking.failed if linking else 0)
        )
        repository.finish_run(
            run_id,
            "OK" if errors == 0 else "WARNING",
            packages_checked=len(plan.packages),
            versions_created=publication.created if publication else 0,
            errors=errors,
        )
        _log_run(
            repository,
            run_id,
            plan=plan,
            publication=publication,
            linking=linking,
            errors=errors,
        )

    if as_json:
        payload = plan.model_dump(mode="json")
        payload["verification"] = (
            verification.model_dump(mode="json") if verification is not None else None
        )
        payload["publication"] = (
            publication.model_dump(mode="json") if publication is not None else None
        )
        payload["linking"] = linking.model_dump(mode="json") if linking is not None else None
        typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        _render_plan(plan, verification, publication, linking)

    raise typer.Exit(code=1 if errors else 0)


def _log_run(
    repository: StateRepository,
    run_id: int,
    *,
    plan: SyncPlan,
    publication: PublicationSummary | None,
    linking: LinkingSummary | None,
    errors: int,
) -> None:
    logger = structlog.get_logger()
    for event in repository.list_events(run_id, "ERROR"):
        logger.error(event.code, run_id=run_id, pkg=event.pkg, detail=event.message)
    logger.info(
        "sync.finished",
        run_id=run_id,
        packages=len(plan.packages),
        updates=plan.updates,
        rejections=plan.rejections,
        versions_created=publication.created if publication else 0,
        versions_linked=linking.linked if linking else 0,
        awaiting_approval=len(linking.awaiting_approval) if linking else 0,
        errors=errors,
    )


@app.command("report")
def report_command(
    runs: Annotated[int, typer.Option("--runs", min=1, help="Executions a afficher.")] = 5,
    as_json: Annotated[bool, typer.Option("--json", help="Sortie JSON.")] = False,
) -> None:
    """Restitue l'historique des executions et les points d'attention."""
    settings = _load_settings()
    with StateRepository(settings.database_path) as repository:
        run_report = build_report(repository, runs=runs)

    if as_json:
        typer.echo(json.dumps(run_report.model_dump(mode="json"), indent=2, ensure_ascii=False))
    else:
        _render_report(run_report)

    raise typer.Exit(code=0 if run_report.healthy or run_report.last_run is None else 1)


@app.command()
def prune(
    days: Annotated[int, typer.Option("--days", min=1, help="Anciennete a conserver.")] = 90,
    assume_yes: Annotated[
        bool, typer.Option("--yes", help="Ne pas demander confirmation.")
    ] = False,
) -> None:
    """Supprime l'historique d'executions anterieur au delai indique."""
    settings = _load_settings()
    before = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    with StateRepository(settings.database_path) as repository:
        if not assume_yes:
            typer.confirm(
                f"Supprimer definitivement les executions anterieures a {before} ?", abort=True
            )
        removed_runs, removed_events = repository.prune_runs(before)

    typer.echo(f"{removed_runs} execution(s) et {removed_events} evenement(s) supprime(s)")


def _render_report(run_report: RunReport) -> None:
    if run_report.last_run is None:
        typer.secho("Aucune execution enregistree.", fg=typer.colors.YELLOW)
        return

    last = run_report.last_run
    typer.secho(
        f"Derniere execution #{last.id}: {last.status}"
        + (" (inachevee)" if run_report.unfinished else ""),
        fg=typer.colors.GREEN if run_report.healthy else typer.colors.RED,
    )
    typer.echo(
        f"  debutee {last.started_at}, {last.packages_checked} paquet(s) verifie(s),"
        f" {last.versions_created} version(s) creee(s), {last.errors} erreur(s)"
    )

    if run_report.errors:
        typer.secho(
            f"\nErreurs de la derniere execution ({len(run_report.errors)}):", fg=typer.colors.RED
        )
        for event in run_report.errors:
            label = f"{event.pkg} " if event.pkg else ""
            typer.echo(f"  {label}{event.code}: {event.message}")

    if run_report.pending_approvals:
        typer.secho(
            f"\nEn attente de rattachement ({len(run_report.pending_approvals)}):",
            fg=typer.colors.YELLOW,
        )
        for pending in run_report.pending_approvals:
            typer.echo(
                f"  {pending.pkg}: version {pending.created_version_code} creee,"
                f" rattachee {pending.pushed_version_code or 'aucune'}"
            )

    typer.echo(f"\nHistorique ({len(run_report.history)} derniere(s) execution(s)):")
    for entry in run_report.history:
        typer.echo(f"  #{entry.id}  {entry.started_at}  {_run_label(entry)}")

    typer.echo(f"\n{run_report.tracked_packages} paquet(s) suivi(s)")


def _run_label(run: SyncRun) -> str:
    return (
        f"{run.status.ljust(8)} {run.packages_checked} verifie(s),"
        f" {run.versions_created} creee(s), {run.errors} erreur(s)"
    )


def _build_sync_plan(
    settings: Settings,
    packages: PackagesFile,
    declared: list[ResolvedPackage],
    repository: StateRepository,
    run_id: int,
) -> SyncPlan:
    with _headwind_client(settings) as client:
        reconciliation = reconcile(declared, client.list_applications(), repository, run_id)
        refresh = _refresh_index(settings, packages, declared, repository, run_id)
        return build_plan(
            declared,
            reconciliation,
            refresh.index,
            client,
            repository,
            run_id=run_id,
            index_source=refresh.source.value,
            index_timestamp=refresh.timestamp,
        )


def _verify_apks(
    settings: Settings,
    packages: PackagesFile,
    plan: SyncPlan,
    repository: StateRepository,
    run_id: int,
) -> VerificationSummary:
    store = ApkStore(settings.cache_dir / "apk")
    with FDroidClient(packages.repo.url, timeout=settings.fdroid_timeout) as client:
        return verify_plan(plan, client, store, repository, run_id=run_id)


def _apply(
    settings: Settings, plan: SyncPlan, repository: StateRepository, run_id: int
) -> tuple[PublicationSummary, LinkingSummary]:
    with _headwind_client(settings) as client:
        publication = publish_plan(plan, client, repository, run_id=run_id)
        return publication, link_plan(plan, client, repository, run_id=run_id)


def _refresh_index(
    settings: Settings,
    packages: PackagesFile,
    declared: list[ResolvedPackage],
    repository: StateRepository,
    run_id: int,
) -> IndexRefresh:
    cache = IndexCache(settings.cache_dir / "index-cache.json")
    with FDroidClient(packages.repo.url, timeout=settings.fdroid_timeout) as client:
        refresh = refresh_index(client, cache, [entry.pkg for entry in declared])
    repository.record_event(
        run_id,
        "INFO",
        "fdroid.index",
        f"Index {refresh.source.value}, timestamp {refresh.timestamp},"
        f" {refresh.package_count} paquet(s) en cache",
    )
    return refresh


def _fail(
    repository: StateRepository, run_id: int, code: str, exc: Exception, message: str
) -> NoReturn:
    repository.record_event(run_id, "ERROR", code, str(exc))
    repository.finish_run(run_id, "FAILED", errors=1)
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=2) from exc


def _render_plan(
    plan: SyncPlan,
    verification: VerificationSummary | None = None,
    publication: PublicationSummary | None = None,
    linking: LinkingSummary | None = None,
) -> None:
    typer.echo(
        f"Index F-Droid: {plan.index_package_count} paquet(s) suivi(s),"
        f" timestamp {plan.index_timestamp} (source {plan.index_source})\n"
    )
    if not plan.packages:
        typer.secho("Aucun paquet declare dans packages.yaml.", fg=typer.colors.YELLOW)
        return

    width = max(len(entry.pkg) for entry in plan.packages)
    for entry in plan.packages:
        typer.secho(
            f"  {entry.pkg.ljust(width)}  {_PLAN_LABEL[entry.status]}",
            fg=_plan_colour(entry.status),
        )
        for line in _plan_details(entry):
            typer.echo(f"  {' ' * width}  {line}")

    typer.echo(
        f"\n{plan.up_to_date} a jour, {plan.updates} mise(s) a jour possible(s),"
        f" {plan.rejections} refus"
    )
    if verification is not None:
        typer.echo(
            f"APK verifies: {verification.verified}, en echec: {verification.failed},"
            f" telecharges: {_human_size(verification.downloaded_bytes)},"
            f" reutilises: {_human_size(verification.reused_bytes)}"
        )
    if publication is None:
        typer.secho("Aucune ecriture effectuee (--dry-run)", fg=typer.colors.BLUE)
        return
    _render_publication(publication)
    if linking is not None:
        _render_linking(linking)


def _render_publication(publication: PublicationSummary) -> None:
    typer.echo("")
    for entry in publication.entries:
        label = f"{entry.pkg} {entry.version or ''}".strip()
        typer.secho(
            f"  {label}: {entry.outcome.value.lower()} - {entry.detail}",
            fg=_publication_colour(entry.outcome),
        )
    typer.secho(
        f"\n{publication.created} version(s) creee(s),"
        f" {publication.skipped} ignoree(s), {publication.failed} en echec",
        fg=typer.colors.RED if publication.failed else typer.colors.GREEN,
    )
    if publication.created:
        typer.secho(
            f"{publication.configurations} configuration(s) referencent ces applications:"
            " celles marquees autoUpdate deploient la nouvelle version sans autre action.",
            fg=typer.colors.YELLOW,
        )


def _render_linking(linking: LinkingSummary) -> None:
    if not linking.entries:
        return
    typer.echo("")
    for entry in linking.entries:
        typer.secho(
            f"  {entry.pkg}: {entry.outcome.value.lower()} - {entry.detail}",
            fg=_linking_colour(entry.outcome),
        )
    typer.secho(
        f"\n{linking.linked} version(s) rattachee(s) a {linking.configurations}"
        f" configuration(s), {linking.skipped} ignoree(s), {linking.failed} en echec",
        fg=typer.colors.RED if linking.failed else typer.colors.GREEN,
    )
    if linking.linked:
        typer.secho(
            "Notification demandee: les appareils ne la recevront que si le service push est"
            " configure, sinon a leur prochaine synchronisation.",
            fg=typer.colors.YELLOW,
        )
    awaiting = linking.awaiting_approval
    if awaiting:
        typer.secho(
            f"En attente d'approbation manuelle (auto_approve: false): {', '.join(awaiting)}",
            fg=typer.colors.YELLOW,
        )


def _linking_colour(outcome: LinkingOutcome) -> str:
    if outcome is LinkingOutcome.LINKED:
        return typer.colors.GREEN
    if outcome is LinkingOutcome.FAILED:
        return typer.colors.RED
    return typer.colors.YELLOW


def _publication_colour(outcome: PublicationOutcome) -> str:
    if outcome is PublicationOutcome.CREATED:
        return typer.colors.GREEN
    if outcome is PublicationOutcome.FAILED:
        return typer.colors.RED
    return typer.colors.YELLOW


def _human_size(size: int) -> str:
    if size < 1024:
        return f"{size} o"
    value = size / 1024
    for unit in ("ko", "Mo"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} Go"


def _plan_details(entry: PackagePlan) -> list[str]:
    if entry.status in (PlanStatus.SKIPPED, PlanStatus.NOT_IN_FDROID):
        return [entry.detail] if entry.detail else []
    if entry.status is PlanStatus.REJECTED:
        lines = [entry.detail or "version non resolue"]
        if entry.signer_state is SignerState.MISMATCH:
            lines.append(f"epingle  {entry.expected_signer}")
            lines.append(f"candidat {entry.candidate_signer}")
        return lines

    lines: list[str] = []
    current = _version_label(entry.headwind_version, entry.headwind_version_code)
    proposed = _version_label(entry.candidate_version, entry.candidate_version_code)
    if entry.status is PlanStatus.UPDATE_AVAILABLE:
        lines.append(f"Headwind {current} -> F-Droid {proposed}")
    else:
        lines.append(f"version {proposed}")

    if entry.split:
        lines.append("publication par ABI:")
        lines.extend(
            f"  {artifact.abi.ljust(12)} versionCode {artifact.version_code}"
            for artifact in entry.artifacts
        )
    if entry.shape_change:
        lines.append(f"changement de structure: split devient {entry.split}")
    if entry.skipped_prereleases:
        lines.append(f"{entry.skipped_prereleases} preversion(s) ecartee(s)")
    if entry.detail:
        lines.append(entry.detail)
    return lines


def _version_label(name: str | None, code: int | None) -> str:
    if name and code is not None:
        return f"{name} ({code})"
    if name:
        return name
    return str(code) if code is not None else "inconnue"


def _plan_colour(plan_status: PlanStatus) -> str:
    if plan_status is PlanStatus.UP_TO_DATE:
        return typer.colors.GREEN
    if plan_status is PlanStatus.UPDATE_AVAILABLE:
        return typer.colors.CYAN
    if plan_status is PlanStatus.REJECTED:
        return typer.colors.RED
    return typer.colors.YELLOW


def _load_settings() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]  # champs fournis par l'environnement FHM_*
    except ValidationError as exc:
        typer.secho(
            f"Configuration d'environnement incomplete:\n{exc}", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(code=2) from exc


def _headwind_client(settings: Settings) -> HeadwindClient:
    return HeadwindClient(
        base_url=settings.headwind_url,
        login=settings.headwind_login,
        password=settings.headwind_password.get_secret_value(),
        timeout=settings.request_timeout,
    )


def _load_packages(settings: Settings) -> PackagesFile:
    try:
        return load_packages_file(settings.packages_file)
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc


def _render(report: ReconciliationReport, application_count: int, show_untracked: bool) -> None:
    typer.echo(f"Applications Headwind: {application_count}")
    typer.echo(f"Paquets suivis: {len(report.packages)}\n")

    if not report.packages:
        typer.secho("Aucun paquet declare dans packages.yaml.", fg=typer.colors.YELLOW)
        return

    width = max(len(entry.pkg) for entry in report.packages)
    for entry in report.packages:
        typer.secho(
            f"  {entry.pkg.ljust(width)}  {_STATUS_LABEL[entry.status]}",
            fg=_colour(entry.status),
        )
        for line in _details(entry):
            typer.echo(f"  {' ' * width}  {line}")

    if report.dropped:
        typer.echo(f"\nRetires du suivi: {', '.join(report.dropped)}")

    if show_untracked and report.untracked_applications:
        typer.echo(f"\nApplications Headwind non suivies ({len(report.untracked_applications)}):")
        for ref in report.untracked_applications:
            typer.echo(f"  #{ref.id} {ref.name}")

    if report.blocking_count:
        typer.secho(
            f"\n{report.blocking_count} paquet(s) requiert une action avant synchronisation.",
            fg=typer.colors.YELLOW,
        )


def _details(entry: PackageReport) -> list[str]:
    if entry.status is PackageStatus.PAUSED and entry.application_id is None:
        unresolved = (
            f"{len(entry.candidates)} applications portent ce package"
            if entry.candidates
            else "absent de Headwind"
        )
        return [f"suivi suspendu, non resolu ({unresolved})"]
    if entry.status is PackageStatus.AMBIGUOUS:
        return [
            f"candidat #{ref.id} {ref.name}"
            + (" (application commune)" if ref.common else "")
            + (f" v{ref.version}" if ref.version else "")
            for ref in entry.candidates
        ]
    if entry.status is PackageStatus.NOT_IN_HEADWIND:
        return ["ajouter l'application dans Headwind avant de la suivre"]

    details = [
        f"application #{entry.application_id}"
        + (f", version {entry.headwind_version}" if entry.headwind_version else "")
    ]
    if entry.expected_signer is None:
        details.append("signataire non epingle")
    if entry.awaiting_approval:
        details.append("version en attente d'approbation")
    return details


def _colour(package_status: PackageStatus) -> str:
    if package_status is PackageStatus.RESOLVED:
        return typer.colors.GREEN
    if package_status is PackageStatus.AMBIGUOUS:
        return typer.colors.RED
    return typer.colors.YELLOW


if __name__ == "__main__":
    app()
