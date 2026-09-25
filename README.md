# fdroid-headwind-mirror

[![CI](https://github.com/Sarcouy/fdroid-headwind-mirror/actions/workflows/ci.yml/badge.svg)](https://github.com/Sarcouy/fdroid-headwind-mirror/actions/workflows/ci.yml)

A service that synchronises F-Droid application updates to [Headwind MDM](https://h-mdm.com/).

The full design is described in [docs/architecture.md](docs/architecture.md), whose section 12.1 details how a
publication unfolds. Fetching the F-Droid index and the rule that selects the candidate version are covered by a
separate note, [docs/iteration-2.md](docs/iteration-2.md).

## Status

| Iteration | Scope | Status |
| --- | --- | --- |
| 1 | Read-only Headwind client, local state, `status` command | ✅ delivered |
| 2 | F-Droid client, version resolution, `sync --dry-run` | ✅ delivered |
| 3 | Verification (sha256, signer, ABI) | ✅ delivered |
| 4 | Publication of a version (direct URL mode) | ✅ delivered |
| 5 | ~~Mirror mode (APK upload)~~ | ❌ abandoned |
| 6 | Linking to the configurations and notification | ✅ delivered |
| 7 | Scheduling, reporting, monitoring | ✅ delivered |

Iteration 5 is abandoned: **Headwind will never host the APKs.** The published versions point to the F-Droid
repository and the devices download them themselves, which assumes they can reach `f-droid.org`. The service
only downloads the APKs to verify their hash before publication.

`sync --apply` is the only command that writes to Headwind. It has never been run against a real instance:
publication and linking are only validated against a simulated server, and no device has received an update
through this path.

## Installation

```bash
poetry install
```

## Configuration

### Environment variables

| Variable | Required | Default | Role |
| --- | --- | --- | --- |
| `FHM_HEADWIND_URL` | yes | — | URL of the Headwind panel, with or without the `/rest` suffix |
| `FHM_HEADWIND_LOGIN` | yes | — | Login of the Headwind service user |
| `FHM_HEADWIND_PASSWORD` | yes | — | Password of that account, exchanged for a JWT at startup |
| `FHM_PACKAGES_FILE` | no | `packages.yaml` | Declarative list of the tracked packages |
| `FHM_DATABASE_PATH` | no | `state.db` | SQLite database of the local state |
| `FHM_CACHE_DIR` | no | `.cache` | Cache of the F-Droid index, projected onto the tracked packages |
| `FHM_REQUEST_TIMEOUT` | no | `30.0` | HTTP timeout towards Headwind, in seconds |
| `FHM_FDROID_TIMEOUT` | no | `300.0` | Timeout towards the F-Droid repository, in seconds |

They can also be placed in a `.env` file at the root.

### Headwind service account

Create a service user in the Headwind panel with the **User** role, not Admin. Both carry
`edit_application_versions`, but "User" leaves out access to the system settings — the interface exposes
roles, not the permissions named in the table of section 5 of the design.

Its name is up to you: the service assumes no particular login and uses the one you declare.

Its login and password are enough: on the first call, the service exchanges them for a JWT on
`POST /rest/public/jwt/login`, then presents that token as `Authorization: Bearer` for the whole run. The
password is never logged and only leaves the process as an MD5 digest, the only format this endpoint accepts.

The `authToken` visible in the database **is not** a login credential: the `/rest/private/*` routes require a
session or a JWT, and presenting it as a `Bearer` answers `HTTP 403`.

### The `packages.yaml` file

Copy [packages.yaml.example](packages.yaml.example) and adapt it. Tracking is **explicitly declarative**: only
the packages listed are taken into account, never the whole F-Droid repository. A package removed from the file
stops being tracked on the next run.

The file can also be written in JSON: since YAML 1.2 is a superset of JSON, a `.json` file is loaded without
conversion. Simply point `FHM_PACKAGES_FILE` at it.

```yaml
repo:
  url: https://f-droid.org/repo
  fingerprint: 43238d512c1e5eb2d6569f4a3afbf5523418b82e0a3ed1552770abb9a9c9ccab

defaults:
  auto_approve: false

packages:
  - pkg: org.mozilla.fennec_fdroid
    auto_approve: true
  - pkg: com.nextcloud.client
  - pkg: org.videolan.vlc
```

| Key | Scope | Role |
| --- | --- | --- |
| `repo.url` | global | Default F-Droid repository |
| `repo.fingerprint` | global | Repository fingerprint — declared but **not verified yet**, see below |
| `auto_approve` | default or package | Link the new version to the configurations automatically — `false` by default |
| `target_abis` | default or package | ABIs to publish, in order of preference — default `[arm64-v8a]` |
| `blocked_anti_features` | default or package | Anti-features that discard a version — empty by default |
| `paused` | package | Suspend tracking without removing the declaration |
| `repo_url` | package | Repository specific to this package |

## Usage

```bash
poetry run fhm status
```

The command matches `packages.yaml` against the applications declared in Headwind:

```
Headwind applications: 4
Tracked packages: 4

  org.mozilla.fennec_fdroid  OK
                             application #7, version 128.0.1
                             signer not pinned
  com.nextcloud.client       AMBIGUOUS
                             candidate #8 Nextcloud v3.29.0
                             candidate #9 Nextcloud (shared) (common application) v3.28.0
  org.videolan.vlc           NOT IN HEADWIND
                             add the application in Headwind before tracking it
```

| Status | Meaning | Expected action |
| --- | --- | --- |
| `OK` | A single Headwind application carries this package | none |
| `PAUSED` | Tracking suspended by `paused: true` | none |
| `NOT IN HEADWIND` | No application carries this package | add the application in Headwind |
| `AMBIGUOUS` | Several applications carry this package | remove the duplicate, or do not track this package |

An `AMBIGUOUS` package is never resolved automatically: associating the wrong application would publish a
version on the wrong record.

`paused: true` silences every alert for this package: it is never counted as blocking, even if it is absent
from Headwind or ambiguous. The details displayed then state that it is not resolved. Pausing a package is
therefore a complete mute, not a mere postponement.

### Options

| Option | Role |
| --- | --- |
| `--json` | Machine-readable output, usable by a monitoring tool |
| `--show-untracked` | Lists the Headwind applications missing from `packages.yaml` |

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | All the tracked packages are resolved |
| `1` | At least one package is `NOT IN HEADWIND` or `AMBIGUOUS` |
| `2` | Configuration error, access denied, or Headwind unreachable |

### Update plan

```bash
poetry run fhm sync --dry-run
```

The command fetches the F-Droid index, resolves the candidate version of each tracked package and compares it
with the one published in Headwind. **`--dry-run` writes nothing to Headwind** — it is the default mode.

```
F-Droid index: 3 tracked package(s), timestamp 1789478586569 (source CACHE)

  org.videolan.vlc           UPDATE AVAILABLE
                             Headwind 3.6.5 (13060506) -> F-Droid 3.7.1 (13070106)
                             per-ABI publication:
                               arm64-v8a    versionCode 13070106
  com.nextcloud.client       UP TO DATE
                             version 34.1.1 (340010190)
                             2 pre-release(s) discarded
  com.pavelsof.wormhole      REJECTED
                             no version for arm64-v8a (available: armeabi-v7a)
```

| Status | Meaning |
| --- | --- |
| `UPDATE AVAILABLE` | A newer version is available on F-Droid |
| `UP TO DATE` | Headwind already carries the candidate version |
| `REJECTED` | Signer mismatch, unavailable ABI, or blocked anti-feature |
| `NOT IN F-DROID` | The package does not exist in the repository |
| `SKIPPED` | Package paused, or not resolved in Headwind (see `status`) |

The only write is local: the signer is pinned on the first successful resolution, and can no longer be
overwritten afterwards. A later mismatch produces a `REJECTED`, since Android rejects any update signed with
another key.

The first call downloads the full index (19 MB gzipped); the following ones start from the projected cache,
which only keeps the tracked packages — 552 kB for three packages, against 60 MB for the whole index.

### APK verification

```bash
poetry run fhm sync --dry-run --verify-apk
```

Without this option, no APK is downloaded: `sync --dry-run` only reads metadata. With it, the APKs of the
packages to update are streamed and their sha256 hash compared with the one in the index.

```
  org.vi_server.red_screen  UPDATE AVAILABLE
                            Headwind 0.1 (0) -> F-Droid 1.2 (3)

0 up to date, 1 update(s) available, 0 rejected
APKs verified: 1, failed: 0, downloaded: 17.2 kB, reused: 0 B
```

The APKs are kept under `FHM_CACHE_DIR/apk/<package>/<versionCode>-<abi>.apk` and reused as long as their
hash remains valid — a tampered file is downloaded again. A hash mismatch at the source rejects the package and
**leaves no file** on disk. The size announced by the index caps the transfer, which keeps a faulty mirror from
filling the cache.

### Publishing to Headwind

```bash
poetry run fhm sync --apply
```

`--apply` creates in Headwind a version pointing directly to the URL of the F-Droid repository. **The devices
download the APK from `f-droid.org` themselves**: this is the chosen distribution mode, and it assumes the fleet
has outgoing access to it. Headwind will never host the APKs.

```
  org.videolan.vlc 3.7.1: created - 2 configuration(s) concerned, latestVersion switched

1 version(s) created, 0 rewritten in place, 0 blocked, 0 skipped, 0 failed
2 configuration(s) reference these applications: those marked autoUpdate deploy the new version
without further action.
```

| Outcome | Meaning |
| --- | --- |
| `created` | The version exists in Headwind, `last_created_version_code` is recorded |
| `blocked` | A Headwind version already carries this name: creating it would rewrite it in place, nothing is written |
| `rewritten` | Headwind answered with a version that already existed: it rewrote it in place instead of creating one |
| `skipped` | APK not verified, version already created, application not resolved, or ABI without a Headwind field |
| `failed` | Unreadable versions or configurations (creation cancelled), or creation refused by Headwind |

A response that is accepted but unusable counts as `created`: Headwind did write, only the returned id is
missing. Treating it as a refusal would republish the version on every run.

Five safeguards frame the write:

- **APK verification is enforced**: `--apply` turns `--verify-apk` on automatically, and an unverified artefact
  is never published. Publishing a URL without having checked the hash of the bytes served would be an
  unfounded integrity claim.
- **The configurations are read before the creation**, since a configuration marked `autoUpdate` switches on
  insertion, and the previous state then stops being observable.
- **Creation is idempotent** on the service side: `last_created_version_code` prevents the same version from
  being published again, since Headwind does not refuse it on its own.
- **`latestVersion` is read again after the creation.** If it has not switched, Headwind propagated nothing —
  its version ranking is textual — and the explicit linking of iteration 6 becomes necessary. The following
  runs keep flagging it (`explicit linking required`) instead of falling back to a silent `skipped`.
- **A version with the same name is never rewritten.** Headwind does not create a version whose name already
  exists: it rewrites the existing one in place, configuration links included, and its devices reinstall it
  without approval. The service therefore reads the versions again right before the write and blocks the
  publication (`blocked`) whatever `auto_approve` says; `--dry-run` already flags the conflict. The conflict is
  resolved in Headwind: edit the existing version yourself, or rename or delete it so that the service creates
  the new one. The details are in the [design](docs/architecture.md#deduplication-by-version-name).

> This command has never been run against a real Headwind instance. It is validated against a simulated server.
> The value of `autoUpdate` on the configurations of the fleet remains unknown: until it is checked, assume that
> creating a version may trigger an immediate deployment. Recommended first use: a staging instance, a single
> package without consequence.

### Linking to the configurations

Once the version is created, `--apply` links it to the configurations that already installed the application,
provided the package carries `auto_approve: true`.

```
  org.videolan.vlc: linked - 2 configuration(s) linked, notification requested

1 version(s) linked to 2 configuration(s), 0 skipped, 0 failed
Notification requested: the devices only receive it if the push service is configured, otherwise at
their next sync.
```

Linking does not depend on the outcome of the plan but on a single state criterion: a version created and not
linked yet. The same rule therefore handles what has just been published and what a previous run left pending —
a package whose version Headwind had not adopted is caught up on the next pass.

| Outcome | Meaning |
| --- | --- |
| `linked` | The configurations point to the new version, notification requested |
| `skipped` | `auto_approve: false`, or no configuration installs this application |
| `failed` | Version not found in Headwind, unreadable links, or linking refused |

Three guarantees frame the write:

- **The entries are sent back as they are.** The API requires each link to come back intact; the service
  therefore reads them without typing them, so as to lose no field and not to convert `versionText`, an integer
  server-side, into a string. Only `action` and `notify` are set.
- **The link is only set where the application is installed.** Headwind does not carry the action over from one
  version to the next: a new version comes back as "do not install" in every configuration. The service
  therefore reads the configurations where a version of the application is installed and only sends links for
  them; an uninstall requested on the version is respected. Like the panel, it never sends a "do not install"
  link, which the server would insert as it is.
- **The notification is requested, not observed.** `notify: true` only triggers a push if the notification
  service is configured on the instance; otherwise the devices pick up the update at their next sync. The API
  does not make it possible to tell the two cases apart.

With `auto_approve: false` — the default — the version is created but left unlinked, and flagged on every run.
Approval then happens in the Headwind interface: the service provides no approval command.

### Reporting and monitoring

```bash
poetry run fhm report
```

Shows the run history and the points needing attention, without any network call — the command only reads the
local state database.

```
Last run #2: OK
  started 2026-09-16T14:38:54+00:00, 3 package(s) checked, 0 version(s) created, 0 error(s)

Awaiting linking (1):
  org.videolan.vlc: version 13070106 created, linked none

History (2 last run(s)):
  #2  2026-09-16T14:38:54+00:00  OK       3 checked, 0 created, 0 error(s)
  #1  2026-09-16T14:38:54+00:00  WARNING  3 checked, 1 created, 1 error(s)

1 tracked package(s)
```

A package awaiting linking remains displayed even when the last run went well: it is a durable state, which
only an operator can clear. The options are `--runs N` and `--json`, and the exit code is `1` if the last run
produced errors.

### Structured log

Each `sync` run emits on **stderr** one JSON line per error, then a summary line:

```json
{"run_id": 3, "packages": 3, "updates": 1, "rejections": 1, "versions_created": 0, "versions_rewritten": 0, "publications_blocked": 0, "versions_linked": 0, "awaiting_approval": 0, "errors": 1, "event": "sync.finished", "level": "info", "timestamp": "2026-09-16T14:38:54Z"}
```

The `--json` report goes to **stdout**, the log to **stderr**: under a timer, `journalctl` collects the latter
without ever making the former impossible to parse.

### Container

```bash
docker build -t fdroid-headwind-mirror .
docker run --rm \
  -v fdroid_mirror_data:/data \
  -v ./packages.yaml:/config/packages.yaml:ro \
  -e FHM_HEADWIND_URL=https://mdm.example.org \
  -e FHM_HEADWIND_LOGIN=fdroid-mirror \
  -e FHM_HEADWIND_PASSWORD=... \
  -e FHM_PACKAGES_FILE=/config/packages.yaml \
  fdroid-headwind-mirror sync --apply
```

The image runs as an unprivileged user and its working directory is `/data`, where the relative default paths
(`state.db`, `.cache`) resolve. **This volume must be persistent**: losing `state.db` means losing the pinned
signers and the two progress counters, hence republishing every tracked package on the next run.

Without arguments, the image runs `sync --dry-run`: an image started by mistake writes nothing to Headwind.

### Publishing the image

A version tag publishes the image to GHCR:

```bash
git tag v0.1.0 && git push origin v0.1.0
```

The workflow refuses to publish if the tag does not match the version declared in `pyproject.toml`: a
mislabelled image would be deployed under a number that does not designate its content. The tags produced are
`0.1.0`, `0.1` and `latest`.

Publication is deliberately tied to tags and not to merges into `main`: the deployment pins an exact version,
and a moving tag on a fleet that updates itself is not desirable.

On the **first** publication, the GHCR package is private: make it public in the repository settings, otherwise
the host will have to authenticate to pull it.

### Daily run

The service embeds no scheduler. The sample units are in [deploy/](deploy):

```bash
sudo install -m 0644 deploy/fdroid-headwind-mirror.{service,timer} /etc/systemd/system/
sudo install -D -m 0640 deploy/env.example /etc/fdroid-headwind-mirror/env
sudo systemctl enable --now fdroid-headwind-mirror.timer
```

Three points the units handle explicitly:

- **The token lives in `EnvironmentFile`**, never in the unit — the unit is world-readable.
- **`WorkingDirectory` is mandatory**: `packages.yaml`, `state.db` and the cache have relative paths by default.
- **`RandomizedDelaySec=2h`** keeps a fleet of services from hitting `f-droid.org` in the same second.

History accumulates on every run. Pruning is an explicit command, never a side effect of a scheduled run:

```bash
poetry run fhm prune --days 90
```

It asks for confirmation before deleting, unless `--yes` is given.

### Chain of trust and known limit

| Link | Verification |
| --- | --- |
| `entry.json` → index / diff | sha256 hash compared before any parsing |
| index → APK | sha256 hash and size compared during the download |
| APK → signer | The signer declared by the index applies to these exact bytes, pinned when tracking starts |
| repository → `entry.json` | **Not verified** — see below |

The `repo.fingerprint` field of `packages.yaml` is **not checked yet**: the service downloads `entry.json`,
which is not signed, and not `entry.jar`. The current protection relies on HTTPS and on the hash chain above.
Validating the fingerprint would require checking a JAR signature, which will be the subject of dedicated work.

The APK certificate is deliberately not re-extracted: if the bytes match the hash in the index, the signer
assertion of the index applies to these bytes. Re-deriving the certificate would only guard against a forged
index — which would declare the signer of the forged APK anyway.

## Fleet inventory

Headwind does not collect the CPU architecture of the devices. To determine which ABIs to publish, the
inventory tool aggregates the enrolled models and Android versions:

```bash
poetry run python tools/parc_inventory.py
```

It uses the same environment variables as the main command. The architecture is then deduced from the model,
or read directly on a device:

```bash
adb shell getprop ro.product.cpu.abilist
```

The fleet targeted here is uniformly `arm64-v8a`.

## Development

```bash
poetry run pytest
poetry run pylint .
poetry run black --check .
```

The test suite is fully offline: HTTP exchanges are simulated with `httpx.MockTransport`, and no Headwind
instance is needed.
