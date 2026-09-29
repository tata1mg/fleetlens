# Security

## Reporting a vulnerability

Report security issues privately through
[GitHub security advisories](https://github.com/tata1mg/fleetlens/security/advisories/new)
rather than opening a public issue.

## What fleetlens does with your code

The deterministic core runs entirely on your machine. It reads source and config files, writes
a SQLite database and a `.context/` directory inside each indexed repository, and makes no
network calls.

Two paths can send code off the machine, and both are opt-in:

* `fl enrich` and `fl index --fill-gaps` with `--provider openai` or another remote endpoint.
  The default provider is Ollama, which runs locally.
* Nothing else. The MCP server reads the database and does not contact anything.

## Config and secrets

fleetlens reads configuration files to resolve service addresses and queue names. It stores
**keys and hostnames**, not values that look like credentials, and `.contextignore` excludes
sensitive files with a fail-closed default (`.env`, `*.pem`, `*.key`, `secrets*`,
`credentials*`, `*password*` and more).

Before sharing a `fleet.db`, be aware it contains service names, endpoint paths, queue names,
config key paths and source file paths from the indexed repositories.
