# The image agent-supplied code runs in. Kept small on purpose: Python,
# bash and coreutils from the base image, plus pandoc. No compiler, no
# curl, no git. The image carries no secrets and no isolation settings;
# every restriction is applied at run time by sandbox_controller. The base is pinned by digest so the
# same build always yields the same sandbox (the tests were validated against this exact image).
#
# Two targets so cold start can be measured with and without pandoc:
#   base -> agent-sandbox:poc-nopandoc      full (default) -> agent-sandbox:poc
ARG BASE=mirror.gcr.io/library/python:3.12-slim-bookworm@sha256:44cf1793efa7c7a476bb0f2cff81443f4a072c90c1cb127e2924933daa3e8c64
FROM ${BASE} AS base
WORKDIR /work

FROM base AS full
COPY build/pandoc /usr/local/bin/pandoc
RUN chmod 0755 /usr/local/bin/pandoc
