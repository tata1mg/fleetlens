# mind1

The cross-repo context your coding agent cannot grep for.

mind1 reads a folder of service repositories and derives the graph between them: which
service calls which over HTTP, who publishes and consumes which queues, and which endpoint
maps to which handler. It serves that graph to an agent over
[MCP](https://modelcontextprotocol.io). The graph is parsed from source, so it needs no
running services, no traces and no hand-maintained catalog.

This directory is the Python package. The project, its documentation and its issue tracker
live at **https://github.com/tata1mg/mind1**.

## Install

mind1 is not on PyPI yet, so install from git and pin a tag:

```bash
pip install "mind1[all] @ git+https://github.com/tata1mg/mind1.git@v0.4.0#subdirectory=packages/mind1"
```

The base install is enough to manage pinned indexes on a remote server; `[all]` adds the
indexing toolchain and the MCP server.

## Use

```bash
mind1 doctor                                    # check the toolchain first
mind1 index-all /path/to/your/repos --db "$PWD/mind1.db"
mind1 serve --db "$PWD/mind1.db"            # stdio, for one engineer
```

Full quickstart, the compatibility matrix, deployment and the benchmark are in the
[project README](https://github.com/tata1mg/mind1#readme).

## License

[Apache-2.0](LICENSE).
