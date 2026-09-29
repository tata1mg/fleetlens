# Running fleetlens for a team

There are two ways to run the server, and the difference is not scale but who owns the
process.

| | transport | who starts it | auth |
|---|---|---|---|
| One engineer, one laptop | stdio | the MCP client | the operating system |
| A team, one shared index | HTTP | you | a shared bearer token |

Start with stdio. Move to HTTP when you want one index built once rather than every
engineer indexing the same repositories on their own machine.

## Try it locally first

No deployment, no token, no container.

```bash
fl index-all /path/to/your/repos
```

```json
{ "mcpServers": { "fleetlens": { "command": "fl", "args": ["serve", "--db", "fleetlens.db"] } } }
```

If that answers useful questions about your fleet, the rest of this page is worth doing.
If it does not, no amount of deployment will fix that.

## Serve a team with Docker

This is the shortest path and it does not care what the host runs.

```bash
export FLEETLENS_TOKEN=$(openssl rand -hex 32)
```

Point `FLEETLENS_REPOS` at your checkouts, build the index, then serve it:

```bash
FLEETLENS_REPOS=/path/to/your/repos docker compose run --rm index
```

```bash
docker compose up -d serve
```

```bash
curl -s localhost:8081/healthz
```

That is the whole deployment. `docker-compose.yml` is in the repository root and is about
thirty lines; read it rather than treating it as magic.

## Serve a team without Docker

Same thing, managed by systemd. Substitute your own paths:

```bash
FLEETLENS_HOME=/opt/fleetlens          # the directory fleetlens owns; the index lives here
FLEETLENS_REPOS=/srv/repos             # your checkouts; can be anywhere
FLEETLENS_USER=fleetlens               # the account that runs it
```

`$FLEETLENS_HOME` is fleetlens's own directory and the index is the only thing it keeps
there. The repositories are separate and must be **writable** by `$FLEETLENS_USER`,
because indexing writes a `.context/` directory into each one. That is the only
non-obvious permission in this setup.

Install:

```bash
python3 -m venv "$FLEETLENS_HOME/venv" && "$FLEETLENS_HOME/venv/bin/pip" install "fleetlens[all]"
```

```bash
sudo npm install -g @sourcegraph/scip-python @sourcegraph/scip-typescript
```

```bash
"$FLEETLENS_HOME/venv/bin/fl" doctor
```

Put the token and the home directory in one env file, so the unit and the refresh job read
the same source:

```bash
printf 'FLEETLENS_HOME=%s\nFLEETLENS_TOKEN=%s\n' "$FLEETLENS_HOME" "$(openssl rand -hex 32)" | sudo tee /etc/fleetlens/env > /dev/null && sudo chmod 640 /etc/fleetlens/env
```

Build the index. With `FLEETLENS_HOME` set there is no `--db` to pass:

```bash
set -a && . /etc/fleetlens/env && set +a && "$FLEETLENS_HOME/venv/bin/fl" index-all "$FLEETLENS_REPOS"
```

`/etc/systemd/system/fleetlens.service`:

```ini
[Unit]
Description=fleetlens MCP server
After=network.target

[Service]
User=fleetlens
EnvironmentFile=/etc/fleetlens/env
ExecStart=/opt/fleetlens/venv/bin/fl serve --http --host 0.0.0.0 --port 8081
Restart=always

# The server only reads the index and never touches the repositories.
ProtectSystem=strict
ReadOnlyPaths=/opt/fleetlens
PrivateTmp=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now fleetlens && curl -s localhost:8081/healthz
```

## Keeping the index fresh

The index is a snapshot. Anything merged after it was built is invisible to every tool on
the server, which is why `get_index_info` exists and why the refresh belongs in a timer
rather than in someone's memory.

Build into a new file and rename it into place. The rename is atomic within a filesystem,
so no client ever reads a half-written index, and **the running server notices the new
file and reopens it on the next request**. There is no restart and no downtime.

```bash
fl index-all "$FLEETLENS_REPOS" --db "$FLEETLENS_HOME/data/fleetlens.db.new" && mv "$FLEETLENS_HOME/data/fleetlens.db.new" "$FLEETLENS_HOME/data/fleetlens.db"
```

Run it nightly after pulling the checkouts. Under Docker the same thing is
`docker compose run --rm index`, which already does the build-and-rename.

## Connecting clients

Engineers need the URL and the token. For Claude Code:

```bash
claude mcp add --transport http fleetlens http://<host>:8081/mcp --header "Authorization: Bearer <token>"
```

For clients configured by file:

```json
{
  "mcpServers": {
    "fleetlens": {
      "type": "http",
      "url": "http://<host>:8081/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

Tell them to call `get_index_info` when an answer looks wrong. Most surprises on a shared
server are staleness, not error.

## What this does not do

Worth reading before you put it somewhere it does not belong.

- **No TLS.** The bearer token travels in cleartext. This is built for a network that is
  already private: a VPN, a VPC, a corporate LAN. On anything reachable from the open
  internet, put a TLS terminator in front of it and do not rely on the token alone.
- **No per-user identity.** One token for the team. Access logs cannot attribute a query
  to an engineer, and revoking one person means rotating for everyone.
- **No rotation without a restart.** The token is read once at startup.
- **One process only.** Streamable-HTTP keeps session state in memory, so a second worker
  would break session affinity. One process is ample: the tools answer from SQLite in
  milliseconds.

If you need per-user identity, replace `BearerAuth` in `fleetlens/server/http.py` with
your identity provider's middleware. Nothing else in the server knows how the caller was
authenticated.
