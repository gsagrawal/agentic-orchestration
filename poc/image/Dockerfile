# The image agent-supplied code runs in. Kept small on purpose: Python,
# bash and coreutils from the base image, plus pandoc. No compiler, no
# curl, no git. The image carries no secrets and no isolation settings;
# every restriction is applied at run time by sandbox_controller.
#
# Two targets so cold start can be measured with and without pandoc:
#   base -> agent-sandbox:poc-nopandoc      full (default) -> agent-sandbox:poc
ARG BASE=mirror.gcr.io/library/python:3.12-slim-bookworm
FROM ${BASE} AS base
WORKDIR /work

FROM base AS full
COPY build/pandoc /usr/local/bin/pandoc
RUN chmod 0755 /usr/local/bin/pandoc
