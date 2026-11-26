# syntax=docker/dockerfile:1
# Image for the proxy and for the stub (the stub only needs `cryptography`, which mitmproxy brings).
FROM mirror.gcr.io/library/python:3.12-slim-bookworm
# BUILD_CA_BUNDLE (optional): a CA file to trust during `pip install`, for build hosts behind a TLS-intercepting proxy.
RUN --mount=type=secret,id=cabundle,required=false \
    if [ -s /run/secrets/cabundle ]; then export PIP_CERT=/run/secrets/cabundle; fi; \
    pip install --no-cache-dir mitmproxy==11.0.2
WORKDIR /app
COPY agent_network_proxy /app/agent_network_proxy
COPY stubs /app/stubs
ENV PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1
