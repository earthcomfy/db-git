# Match the GitHub-hosted Linux runner and provide every matrix client version.
FROM ghcr.io/astral-sh/uv:0.12.19 AS uv
FROM docker:cli AS docker
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg git mysql-client && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    | gpg --dearmor -o /usr/share/keyrings/postgresql.gpg \
    && echo 'deb [signed-by=/usr/share/keyrings/postgresql.gpg] https://apt.postgresql.org/pub/repos/apt noble-pgdg main' \
    > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client-13 postgresql-client-14 postgresql-client-15 \
    postgresql-client-16 postgresql-client-17 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /uvx /usr/local/bin/
COPY --from=docker /usr/local/bin/docker /usr/local/bin/
RUN mkdir /workspace && chown ubuntu:ubuntu /workspace
USER ubuntu
ENV PATH="/home/ubuntu/.local/bin:${PATH}"
RUN uv python install 3.12 3.13 && uv tool install nox
WORKDIR /workspace
