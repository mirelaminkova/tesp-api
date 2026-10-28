# Uses multi-stage builds requiring Docker 17.05 or higher
# See https://docs.docker.com/develop/develop-images/multistage-build/

# Environment variables and base image
# Debian bullseye is EOL (its security repo 404s); pinned by digest for reproducible builds.
# Bump: docker buildx imagetools inspect python:3.12-slim-bookworm
# Apptainer publishes amd64 .deb files only, so this image is amd64-only (compose sets the platform).
FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337 as python-base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=off \
    PIP_DISABLE_PIP_VERSION_CHECK=on \
    PIP_DEFAULT_TIMEOUT=100 \
    PYSETUP_PATH="/opt/pysetup" \
    VENV_PATH="/opt/pysetup/.venv"

ENV PATH="$VENV_PATH/bin:$PATH"


# Builder is used to install shared depenedencies
FROM python-base as builder
WORKDIR $PYSETUP_PATH
RUN python3 -m venv $VENV_PATH && . .venv/bin/activate
ARG PULSAR_VERSION=0.16.0
RUN pip install "pulsar-app[web]==${PULSAR_VERSION}"


# Development installs all dependencies and can be used
# to develope code.
FROM python-base as development
COPY --from=builder $PYSETUP_PATH $PYSETUP_PATH

# Install dependencies required by Apptainer (Singularity)
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    curl \
    gnupg \
    lsb-release \
    wget \
    libseccomp2 \
    uidmap \
    squashfs-tools \
    squashfuse \
    fuse2fs \
    fuse-overlayfs \
    fakeroot \
    cryptsetup

# Download and install Apptainer. Upstream ships no checksums, so the sha256 is pinned here;
# update both together.
ARG APPTAINER_VERSION=1.3.6
ARG APPTAINER_SHA256=2723b2928cfc30edf687723c49556ec4e013f0bf7cdb43a5a76bca7bd3c70792
RUN arch="$(dpkg --print-architecture)" \
    && if [ "$arch" != amd64 ]; then echo "Apptainer publishes amd64 .deb files only, not $arch" >&2; exit 1; fi \
    && curl -fsSLO https://github.com/apptainer/apptainer/releases/download/v${APPTAINER_VERSION}/apptainer_${APPTAINER_VERSION}_amd64.deb \
    && echo "${APPTAINER_SHA256}  apptainer_${APPTAINER_VERSION}_amd64.deb" | sha256sum -c - \
    && apt-get install -y ./apptainer_${APPTAINER_VERSION}_amd64.deb \
    && rm apptainer_${APPTAINER_VERSION}_amd64.deb

# On bookworm, add-apt-repository writes an entry apt does not pick up and
# apt-key is deprecated, so use a dedicated keyring with signed-by.
RUN install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(lsb_release -cs) stable" \
       > /etc/apt/sources.list.d/docker.list
RUN apt-get update
RUN apt-get install -y docker-ce docker-ce-cli containerd.io
RUN apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR $PYSETUP_PATH
COPY startup.sh startup.sh
RUN pulsar-config --host 0.0.0.0
EXPOSE 8913
CMD ["/bin/bash", "./startup.sh"]
