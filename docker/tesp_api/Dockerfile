# Uses multi-stage builds requiring Docker 17.05 or higher
# See https://docs.docker.com/develop/develop-images/multistage-build/

# Environment variables and base image
# Debian bullseye is EOL (its security repo 404s); pinned by digest for reproducible builds.
# Bump: docker buildx imagetools inspect python:3.10-slim-bookworm
FROM python:3.10-slim-bookworm@sha256:5be5aaecb962a41567bc8041f1e9e3432cb085b037fe057f9a562630b2e4b370 as python-base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=off \
    PIP_DISABLE_PIP_VERSION_CHECK=on \
    PIP_DEFAULT_TIMEOUT=100 \
    POETRY_HOME="/opt/poetry" \
    POETRY_VIRTUALENVS_IN_PROJECT=true \
    POETRY_NO_INTERACTION=1 \
    PYSETUP_PATH="/opt/pysetup" \
    VENV_PATH="/opt/pysetup/.venv"

ENV PATH="$POETRY_HOME/bin:$VENV_PATH/bin:$PATH"


# Builder is used to install shared depenedencies
FROM python-base as builder
RUN buildDeps="build-essential" \
    && apt-get update \
    && apt-get install --no-install-recommends -y curl \
    && apt-get install -y --no-install-recommends $buildDeps \
    && rm -rf /var/lib/apt/lists/*

# Install poetry, pinned. Poetry 1.1.x breaks with current `packaging`
# (canonicalize_version() got an unexpected keyword 'strip_trailing_zero').
ENV POETRY_VERSION=1.8.5
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN python3 -m venv $POETRY_HOME \
    && $POETRY_HOME/bin/pip install "poetry==$POETRY_VERSION"

# Copy python requirements and install runtime deps
WORKDIR $PYSETUP_PATH
#COPY ./poetry.lock ./pyproject.toml ./
COPY ./pyproject.toml ./
RUN poetry install --no-dev --no-root


# Development installs all dependencies and can be used
# to develope code. With local volume mapeed under /app
# hot reloading (change code in running app) can be used
FROM python-base as development
ENV FASTAPI_PROFILE=dev-docker

COPY --from=builder $POETRY_HOME $POETRY_HOME
COPY --from=builder $PYSETUP_PATH $PYSETUP_PATH

WORKDIR $PYSETUP_PATH
RUN poetry install --no-root
WORKDIR /app
COPY . .

EXPOSE 8080
CMD ["uvicorn", "--reload", "--host=0.0.0.0", "--port=8080", "tesp_api.tesp_api:app"]
