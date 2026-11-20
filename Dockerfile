# mind1 in one artifact.
#
# Indexing spans two ecosystems: Python for mind1 itself, Node for the SCIP indexers that
# build the call graph. This image carries both so trying it out needs nothing installed on
# the host.
#
#   docker build -t mind1 .
#
# Index a folder of repositories. Mount them read-write: mind1 writes a .context/
# directory into each one.
#
#   docker run --rm -v "$PWD/repos:/repos" -v mind1-data:/data \
#       mind1 index-all /repos
#
# Serve the result to a team over HTTP. See docker-compose.yml for the same thing with
# restart policy and a health check attached.
#
#   docker run -d -p 8081:8081 -v mind1-data:/data -e MIND1_TOKEN \
#       mind1 serve --http --host 0.0.0.0
#
# For a single engineer, `mind1 serve` over stdio on the host is simpler than a container:
# the MCP client wants to start the process itself.
#
# Behind a TLS-intercepting corporate proxy, npm and pip cannot verify the registry
# certificate and both install steps fail. Mount your organisation's CA rather than
# disabling verification:
#
#   docker build --build-arg EXTRA_CA=corp-root.crt -t mind1 .

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
COPY packages/mind1/pyproject.toml ./packages/mind1/
COPY packages/mind1/mind1 ./packages/mind1/mind1
COPY README.md LICENSE ./
RUN ln -sf ../../README.md packages/mind1/README.md \
    && ln -sf ../../LICENSE packages/mind1/LICENSE \
    && pip install --no-cache-dir "./packages/mind1[all]"

# One directory for everything mind1 owns, so a single volume carries the index between
# the indexing container and the serving one. Repositories are mounted separately.
ENV MIND1_HOME=/data
VOLUME /data
EXPOSE 8081

# Indexing writes a .context/ directory into each repository it reads, so mount your sources
# read-write, or accept that the call graph step will fail on a read-only mount.
WORKDIR /repos
ENTRYPOINT ["mind1"]
CMD ["doctor"]
