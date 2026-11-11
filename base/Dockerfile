FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends bash curl ca-certificates git ripgrep file xxd binutils procps less unzip xz-utils \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    HOME=/tmp/home \
    PYTHONPATH=/harness-common

RUN mkdir -p /tmp/home && chmod 777 /tmp/home

WORKDIR /workspace
