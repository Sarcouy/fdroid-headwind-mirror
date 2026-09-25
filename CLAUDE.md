# CLAUDE.md

Instructions specific to this repository. They complement the global configuration and, on the points below,
override it.

## Git — never push directly to `main`

**Never push to `origin/main`.** No exception, not even for a one-line fix, a documentation update or a red
pipeline.

Every change follows this path:

1. create a branch from an up-to-date `main` — `git switch -c <type>/<topic>` (`feat/`, `fix/`, `docs/`,
   `chore/`);
2. commit on that branch;
3. push the branch — `git push -u origin <branch>`;
4. open a pull request — `gh pr create`;
5. wait for the CI to be green, then leave the merge to the repository author.

Never merge a PR without having been explicitly asked to.

## Pull requests

- Describe what the PR changes **and why** these choices were made.
- Start with a TL;DR section, as short as possible.
- Open the PR as a draft (`--draft`).
- Assign the repository author as assignee and reviewer.
- End the description with the `:factory:` emoji.
- Write the title and the description in English.

No `/spend`: it is a GitLab quick action, with no effect on GitHub. The MCP server to use here is **github**,
not gitlab, despite the global rule targeting `~/workspace/**`.

## Public repository

This repository is public. Before any commit: no secret, no token, no internal domain name, no fleet data.
`.gitignore` excludes `.env`, `state.db`, `packages.yaml`, `.cache/` and `.venv/` — check that nothing
sensitive has been added to the diff.

## Checks before committing

The three CI checks, in the Poetry environment:

```bash
poetry run pytest -q
poetry run black --check .
poetry run pylint fdroid_headwind_mirror tests tools
```

**Read the exit codes, never the output alone.** Pylint displays "10.00/10" while exiting with a non-zero code
when it has emitted a message: the score is rounded and does not reflect the presence of a warning. A `| tail`
reports the status of `tail`, not that of the linter — do not pipe these commands. This mistake has already
passed a red pipeline off as green.

The packages are targeted explicitly in the pylint call: without that, it walks `.venv` in CI.

## Project conventions

- Code, identifiers, commit messages and documentation in English — the repository is public.
- Comments are reserved for decisions that cannot be deduced from the code, and must then be self-sufficient.
- Strict type annotations.
- Fully offline tests: `httpx.MockTransport` for Headwind as well as for F-Droid.
- Every write to Headwind stays behind `sync --apply`, never active in `--dry-run`.
