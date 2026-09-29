# fleetlens in one artifact.
#
# Indexing spans two ecosystems: Python for fleetlens itself, Node for the SCIP indexers that
# build the call graph. This image carries both so trying it out needs nothing installed on
# the host.
#
#   docker build -t fleetlens .
#
# Index a folder of repositories. Mount them read-write: fleetlens writes a .context/
# directory into each one.
#
#   docker run --rm -v "$PWD/repos:/repos" -v fleetlens-data:/data \
#       fleetlens index-all /repos
#
# Serve the result to a team over HTTP. See docker-compose.yml for the same thing with
# restart policy and a health check attached.
#
#   docker run -d -p 8081:8081 -v fleetlens-data:/data -e FLEETLENS_TOKEN \
#       fleetlens serve --http --host 0.0.0.0
#
# For a single engineer, `fl serve` over stdio on the host is simpler than a container:
# the MCP client wants to start the process itself.
#
# Behind a TLS-intercepting corporate proxy, npm and pip cannot verify the registry
# certificate and both install steps fail. Mount your organisation's CA rather than
# disabling verification:
#
#   docker build --build-arg EXTRA_CA=corp-root.crt -t fleetlens .

FROM python:3.12-slim

# Optional: an extra root CA, for networks that intercept TLS. Left empty by default.
ARG EXTRA_CA=""
COPY ${EXTRA_CA:-Dockerfile} /tmp/extra-ca-source
RUN if [ -n "$EXTRA_CA" ]; then \
        cp /tmp/extra-ca-source /usr/local/share/ca-certificates/extra.crt \
        && update-ca-certificates \
        && echo "NODE_EXTRA_CA_CERTS=/usr/local/share/ca-certificates/extra.crt" >> /etc/environment; \
    fi

# Node provides scip-python and scip-typescript. git is needed because some indexers inspect
# repository metadata.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm git ca-certificates \
    && npm install -g @sourcegraph/scip-python @sourcegraph/scip-typescript \
    && apt-get purge -y npm \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY packages/fleetlens/pyproject.toml ./packages/fleetlens/
COPY packages/fleetlens/fleetlens ./packages/fleetlens/fleetlens
COPY README.md LICENSE ./
RUN ln -sf ../../README.md packages/fleetlens/README.md \
    && ln -sf ../../LICENSE packages/fleetlens/LICENSE \
    && pip install --no-cache-dir "./packages/fleetlens[all]"

# One directory for everything fleetlens owns, so a single volume carries the index between
# the indexing container and the serving one. Repositories are mounted separately.
ENV FLEETLENS_HOME=/data
VOLUME /data
EXPOSE 8081

# Indexing writes a .context/ directory into each repository it reads, so mount your sources
# read-write, or accept that the call graph step will fail on a read-only mount.
WORKDIR /repos
ENTRYPOINT ["fl"]
CMD ["doctor"]
