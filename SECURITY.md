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
* Nothing else. The MCP server makes no outbound calls of its own, unless it is configured
  with a remote embedding provider for the semantic search tools.

## The HTTP server

`fl serve --http` is the only part of fleetlens that listens on a network, and it is built
for a trusted network rather than for the open internet. Know these before you expose it:

* **No TLS.** Requests and the bearer token travel in the clear. Put it behind a reverse
  proxy that terminates TLS, or keep it on a private network.
* **One shared token for the whole team.** There is no per-user identity, so a request
  cannot be attributed to a person, and access cannot be revoked for one person without
  rotating the token for everyone.
* **Rotation needs a restart.** The token is read once at startup.
* **Pinning changes state.** `POST /pins` and `DELETE /pins/<alias>` create and remove
  snapshots of the index on the server's disk. They sit behind the same shared token, so
  anyone who can read can also pin and unpin, and a pin is a full copy of the index.
* **`--insecure` turns authentication off entirely**, including for the pin endpoints. It
  is for a local socket, not a shared host. The server logs a warning when it is used.

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
