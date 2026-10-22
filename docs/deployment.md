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
fl index-all /path/to/your/repos --db "$PWD/fleetlens.db"
```

Absolute paths in the client config, for both the executable and the index. The client
starts `fl` in a working directory of its own and without your shell's `PATH`, so a
relative `--db` points somewhere else and a bare `fl` may not be found at all.

```json
{
  "mcpServers": {
    "fleetlens": {
      "command": "/absolute/path/to/fl",
      "args": ["serve", "--db", "/absolute/path/to/fleetlens.db"]
    }
  }
}
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
fifty lines; read it rather than treating it as magic.

## Serve a team without Docker

Same thing, managed by systemd. Substitute your own paths:

```bash
FLEETLENS_HOME=/opt/fleetlens          # the directory fleetlens owns; the index lives here
FLEETLENS_REPOS=/srv/repos             # your checkouts; can be anywhere
FLEETLENS_USER=fleetlens               # the account that runs it
```

`$FLEETLENS_HOME` is fleetlens's own directory. The index lives there, and so do pin
snapshots, so it has to be writable by the account that serves. The repositories are
separate and must **also** be writable by `$FLEETLENS_USER`, because indexing writes a
`.context/` directory into each one. Those two are the only non-obvious permissions here.

Create the account and the directories first. The unit below runs as `$FLEETLENS_USER`, so
anything it writes has to belong to that account:

```bash
sudo useradd --system --home-dir "$FLEETLENS_HOME" --shell /usr/sbin/nologin "$FLEETLENS_USER" || true
sudo mkdir -p "$FLEETLENS_HOME"
sudo chown -R "$FLEETLENS_USER:$FLEETLENS_USER" "$FLEETLENS_HOME" "$FLEETLENS_REPOS"
```

Install **as that account**. The directory belongs to it now, so building the virtualenv as
yourself fails on a fresh machine, and where it does succeed it leaves a tree the service
cannot update:

```bash
sudo -u "$FLEETLENS_USER" python3 -m venv "$FLEETLENS_HOME/venv"
sudo -u "$FLEETLENS_USER" "$FLEETLENS_HOME/venv/bin/pip" install \
  "fleetlens[all] @ git+https://github.com/tata1mg/fleetlens.git@v0.2.0#subdirectory=packages/fleetlens"
```

fleetlens is not on PyPI, so the install is from git. Pin the tag rather than tracking
`main`: an index built by one version is not guaranteed to be readable by another.

```bash
sudo npm install -g @sourcegraph/scip-python @sourcegraph/scip-typescript
```

```bash
sudo -u "$FLEETLENS_USER" "$FLEETLENS_HOME/venv/bin/fl" doctor
```

Put the token and the paths in one env file next to the index, so the server and the refresh
timer read the same source. `FLEETLENS_REPOS` goes in because nothing else tells the timer
where the checkouts are. systemd reads this file as root before dropping privileges, so it
never has to be readable by you:

```bash
printf 'FLEETLENS_HOME=%s\nFLEETLENS_REPOS=%s\nFLEETLENS_TOKEN=%s\n' \
  "$FLEETLENS_HOME" "$FLEETLENS_REPOS" "$(openssl rand -hex 32)" \
  | sudo -u "$FLEETLENS_USER" tee "$FLEETLENS_HOME/.env" > /dev/null
sudo chmod 600 "$FLEETLENS_HOME/.env"
```

Build the first index, as the same account. Indexing needs `FLEETLENS_HOME` and nothing
else, so there is no env file to source here and the token stays where it is:

```bash
sudo -u "$FLEETLENS_USER" env FLEETLENS_HOME="$FLEETLENS_HOME" \
  "$FLEETLENS_HOME/venv/bin/fl" index-all "$FLEETLENS_REPOS"
```

With `FLEETLENS_HOME` set there is no `--db` to pass: the index lands at
`$FLEETLENS_HOME/data/fleetlens.db`, which is where the units below look for it.

`/etc/systemd/system/fleetlens.service`:

```ini
[Unit]
Description=fleetlens MCP server
After=network.target

[Service]
User=fleetlens
EnvironmentFile=/opt/fleetlens/.env
ExecStart=/opt/fleetlens/venv/bin/fl serve --http --host 0.0.0.0 --port 8081
Restart=always

# The server never touches the repositories. It reads the index and, when someone pins a
# build, writes a snapshot of it next to the index, so its own directory must stay writable.
ProtectSystem=strict
ReadWritePaths=/opt/fleetlens
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

Under Docker the same thing is `docker compose run --rm index`, which already does the
build-and-rename.

On systemd, put it in a timer rather than in someone's memory. Two units, both reading the
same env file as the server:

`/etc/systemd/system/fleetlens-reindex.service`:

```ini
[Unit]
Description=Rebuild the fleetlens index
After=network.target

[Service]
Type=oneshot
User=fleetlens
EnvironmentFile=/opt/fleetlens/.env
ExecStart=/bin/sh -c '/opt/fleetlens/venv/bin/fl index-all "$FLEETLENS_REPOS" --db "$FLEETLENS_HOME/data/fleetlens.db.new" && mv "$FLEETLENS_HOME/data/fleetlens.db.new" "$FLEETLENS_HOME/data/fleetlens.db"'
```

`/etc/systemd/system/fleetlens-reindex.timer`:

```ini
[Unit]
Description=Rebuild the fleetlens index nightly

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now fleetlens-reindex.timer
```

Pull the checkouts before it runs, from the same timer or whatever already updates them.
The index is only as current as the working copies it read.

## Pinning an index

`fl pin` freezes the served index as a named snapshot for a repeatable evaluation. The
commands and the full guide are in [pinning.md](pinning.md).

The server needs a writable directory for the snapshots: `pins/` next to the index by
default, or wherever `--pins-dir` says. Under Compose this is already set up, with the
index volume still read-only and a separate `fleetlens-pins` volume mounted at `/pins`, so
pinning works out of the box and the server still cannot write the index. Change it only if
you want the snapshots somewhere specific, by repointing that volume.

On the systemd deployment the snapshots land under `$FLEETLENS_HOME`, which is why the unit
above sets `ReadWritePaths` rather than `ReadOnlyPaths`.

Two environment variables set retention: `FLEETLENS_RETAIN_PINNED_DAYS` (default 30) and
`FLEETLENS_PIN_EXTEND_MAX_DAYS` (default 10, the most one extension may add). The pin routes
use the same token as `/mcp`.

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
