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

fleetlens reads configuration files to work out which services address which. That includes
`.env`, because `ORDERS_SERVICE_HOST=orders-svc` is exactly the fact it is looking for.

Connection strings routinely carry a password in the middle of an address, so the credential
is stripped before anything is stored: `postgres://admin:hunter2@db.internal:5432/orders` is
kept as `postgres://db.internal:5432/orders`. The host and port survive, the secret does not.
Values that are wholly opaque are stored as `[redacted]`.

Some files are never read at all, whatever a repo's configuration says:

```
*.pem  *.key  *.p12  *.pfx  *.jks  *.keystore
id_rsa  id_dsa  id_ecdsa  id_ed25519
*secret*  *credential*  *password*
.npmrc  .pypirc  .netrc  .htpasswd
```

A repo can exclude more by listing patterns in `.fleetlensignore`, one per line, gitignore
style. That includes `.env` itself if you would rather fleetlens did not read it, at the cost
of the service addresses declared there. A repo can add to this set; it cannot remove
anything from the list above.

Before sharing a `fleet.db`, be aware it contains service names, endpoint paths, queue names,
config key paths, hostnames, ports and source file paths from the indexed repositories.
