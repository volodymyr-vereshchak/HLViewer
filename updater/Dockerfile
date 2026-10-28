# The updater: git + the docker CLI with the compose and buildx plugins, and a
# small FastAPI app. It drives the host's Docker through the mounted socket,
# so the daemon is never installed here — only the client binaries, copied
# from the official CLI image.
FROM docker:27.5.1-cli AS docker-cli

FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/docker-buildx /usr/local/libexec/docker/cli-plugins/docker-buildx

# The checkouts belong to the host user, git here runs as root: without this
# every command refuses with "detected dubious ownership".
RUN git config --system --add safe.directory '*'

WORKDIR /srv
COPY requirements.txt /srv/updater/requirements.txt
RUN pip install --no-cache-dir -r /srv/updater/requirements.txt
COPY . /srv/updater

EXPOSE 8090
# One worker, on purpose: the running job and its lock live in this process.
CMD ["uvicorn", "updater.main:app", "--host", "0.0.0.0", "--port", "8090", "--workers", "1"]
