FROM python:3.12-slim AS build

ARG POETRY_VERSION=2.3.2
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_NO_INTERACTION=1

# Poetry is installed before PATH points at the virtualenv, so it lands in the
# system interpreter and never ends up inside the runtime image.
RUN pip install "poetry==${POETRY_VERSION}"

# The virtualenv is built at its final path: console script shebangs are
# absolute, so a venv created elsewhere and copied would point at a directory
# that does not exist in the runtime stage.
RUN python -m venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:${PATH}" \
    POETRY_VIRTUALENVS_CREATE=false

WORKDIR /src
COPY pyproject.toml poetry.lock ./
COPY fdroid_headwind_mirror ./fdroid_headwind_mirror
# --no-root then a plain pip install: `poetry install` would add the project
# itself in editable mode, which only leaves a .pth pointing at /src — a path
# the runtime stage does not carry. Dependencies still come from the lock file.
RUN poetry install --only main --no-root \
 && pip install --no-deps --no-cache-dir .


FROM python:3.12-slim

LABEL org.opencontainers.image.title="fdroid-headwind-mirror" \
      org.opencontainers.image.description="Synchronise F-Droid application updates to Headwind MDM" \
      org.opencontainers.image.source="https://github.com/Sarcouy/fdroid-headwind-mirror" \
      org.opencontainers.image.licenses="MIT"

# The default paths of packages.yaml, state.db and .cache are relative, so the
# working directory is where the data volume is expected to be mounted.
RUN useradd --system --create-home --home-dir /data --shell /usr/sbin/nologin fhm

COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1

WORKDIR /data
USER fhm

# No default write: an image started without arguments reports what it would do
# and touches nothing in Headwind.
ENTRYPOINT ["fhm"]
CMD ["sync", "--dry-run"]
