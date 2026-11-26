# syntax=docker/dockerfile:1
# Image for the proxy and for the stub (the stub only needs `cryptography`, which mitmproxy brings).
ARG BASE=mirror.gcr.io/library/python:3.12-slim-bookworm@sha256:44cf1793efa7c7a476bb0f2cff81443f4a072c90c1cb127e2924933daa3e8c64
FROM ${BASE}
# BUILD_CA_BUNDLE (optional): a CA file to trust during `pip install`, for build hosts behind a TLS-intercepting proxy.
RUN --mount=type=secret,id=cabundle,required=false \
    if [ -s /run/secrets/cabundle ]; then export PIP_CERT=/run/secrets/cabundle; fi; \
    pip install --no-cache-dir mitmproxy==11.0.2
WORKDIR /app
COPY agent_network_proxy /app/agent_network_proxy
COPY stubs /app/stubs
ENV PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1
