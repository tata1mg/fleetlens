# Running mind1 for a team

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
mind1 index-all /path/to/your/repos --db "$PWD/mind1.db"
```

Absolute paths in the client config, for both the executable and the index. The client
starts `mind1` in a working directory of its own and without your shell's `PATH`, so a
relative `--db` points somewhere else and a bare `mind1` may not be found at all.

```json
{
  "mcpServers": {
    "mind1": {
      "command": "/absolute/path/to/mind1",
      "args": ["serve", "--db", "/absolute/path/to/mind1.db"]
    }
  }
}
```

If that answers useful questions about your fleet, the rest of this page is worth doing.
If it does not, no amount of deployment will fix that.

## Serve a team with Docker

This is the shortest path and it does not care what the host runs.

```bash
export MIND1_TOKEN=$(openssl rand -hex 32)
```

Point `MIND1_REPOS` at your checkouts, build the index, then serve it:

```bash
MIND1_REPOS=/path/to/your/repos docker compose run --rm index
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

Same thing, managed by systemd. Set these three once, in the shell you run the rest of this
section in. Every command below, the unit files included, is written in terms of them, so
changing a path here is the only edit you have to make:

```bash
MIND1_HOME=/opt/mind1          # the directory mind1 owns; the index lives here
MIND1_REPOS=/srv/repos             # your checkouts; can be anywhere
MIND1_USER=mind1               # the account that runs it
```

`$MIND1_HOME` is mind1's own directory. The index lives there, and so do pin
snapshots, so it has to be writable by the account that serves. The repositories are
separate and must **also** be writable by `$MIND1_USER`, because indexing writes a
`.context/` directory into each one. Those two are the only non-obvious permissions here.

Create the account and the directories first. The unit below runs as `$MIND1_USER`, so
anything it writes has to belong to that account:

```bash
sudo useradd --system --home-dir "$MIND1_HOME" --shell /usr/sbin/nologin "$MIND1_USER" || true
sudo mkdir -p "$MIND1_HOME"
sudo chown -R "$MIND1_USER:$MIND1_USER" "$MIND1_HOME" "$MIND1_REPOS"
```

Install **as that account**. The directory belongs to it now, so building the virtualenv as
yourself fails on a fresh machine, and where it does succeed it leaves a tree the service
cannot update:

```bash
sudo -u "$MIND1_USER" python3 -m venv "$MIND1_HOME/venv"
sudo -u "$MIND1_USER" "$MIND1_HOME/venv/bin/pip" install \
  "mind1[all] @ git+https://github.com/tata1mg/mind1.git@v0.3.1#subdirectory=packages/mind1"
```

mind1 is not on PyPI, so the install is from git. Pin the tag rather than tracking
`main`: an index built by one version is not guaranteed to be readable by another.

```bash
sudo npm install -g @sourcegraph/scip-python @sourcegraph/scip-typescript
```

```bash
sudo -u "$MIND1_USER" "$MIND1_HOME/venv/bin/mind1" doctor
```

Put the token and the paths in one env file next to the index, so the server and the refresh
timer read the same source. `MIND1_REPOS` goes in because nothing else tells the timer
where the checkouts are. systemd reads this file as root before dropping privileges, so it
never has to be readable by you:

```bash
printf 'MIND1_HOME=%s\nMIND1_REPOS=%s\nMIND1_TOKEN=%s\n' \
  "$MIND1_HOME" "$MIND1_REPOS" "$(openssl rand -hex 32)" \
  | sudo -u "$MIND1_USER" tee "$MIND1_HOME/.env" > /dev/null
sudo chmod 600 "$MIND1_HOME/.env"
```

Build the first index, as the same account. Indexing needs `MIND1_HOME` and nothing
else, so there is no env file to source here and the token stays where it is:

```bash
sudo -u "$MIND1_USER" env MIND1_HOME="$MIND1_HOME" \
  "$MIND1_HOME/venv/bin/mind1" index-all "$MIND1_REPOS"
```

With `MIND1_HOME` set there is no `--db` to pass: the index lands at
`$MIND1_HOME/data/mind1.db`, which is where the units below look for it.

Now the unit. A systemd unit is not a shell script: the executable in `ExecStart=` has to be
a literal absolute path, and `EnvironmentFile=` and `ReadWritePaths=` are read before the env
file they point at, so none of them can say `$MIND1_HOME`. Rather than hand-substitute
the same path in four places, write the file with the shell and let it fill them in:

```bash
sudo tee /etc/systemd/system/mind1.service > /dev/null <<EOF
[Unit]
Description=mind1 MCP server
After=network.target

[Service]
User=$MIND1_USER
EnvironmentFile=$MIND1_HOME/.env
ExecStart=$MIND1_HOME/venv/bin/mind1 serve --http --host 0.0.0.0 --port 8081
Restart=always

# The server never touches the repositories. It reads the index and, when someone pins a
# build, writes a snapshot of it next to the index, so its own directory must stay writable.
ProtectSystem=strict
ReadWritePaths=$MIND1_HOME
PrivateTmp=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now mind1 && curl -s localhost:8081/healthz
```

## Keeping the index fresh

The index is a snapshot. Anything merged after it was built is invisible to every tool on
the server, which is why `get_index_info` exists and why the refresh belongs in a timer
rather than in someone's memory.

Build into a new file and rename it into place. The rename is atomic within a filesystem,
so no client ever reads a half-written index, and **the running server notices the new
file and reopens it on the next request**. There is no restart and no downtime.

```bash
mind1 index-all "$MIND1_REPOS" --db "$MIND1_HOME/data/mind1.db.new" && mv "$MIND1_HOME/data/mind1.db.new" "$MIND1_HOME/data/mind1.db"
```

Under Docker the same thing is `docker compose run --rm index`, which already does the
build-and-rename.

On systemd, put it in a timer rather than in someone's memory. Two units, both reading the
same env file as the server:

The escaped `\$` below are deliberate: those stay in the file so the shell expands them from
the env file at run time, while `$MIND1_HOME` and `$MIND1_USER` are filled in now.

```bash
sudo tee /etc/systemd/system/mind1-reindex.service > /dev/null <<EOF
[Unit]
Description=Rebuild the mind1 index
After=network.target

[Service]
Type=oneshot
User=$MIND1_USER
EnvironmentFile=$MIND1_HOME/.env
ExecStart=/bin/sh -c '$MIND1_HOME/venv/bin/mind1 index-all "\$MIND1_REPOS" --db "\$MIND1_HOME/data/mind1.db.new" && mv "\$MIND1_HOME/data/mind1.db.new" "\$MIND1_HOME/data/mind1.db"'
EOF
```

```bash
sudo tee /etc/systemd/system/mind1-reindex.timer > /dev/null <<'EOF'
[Unit]
Description=Rebuild the mind1 index nightly

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
EOF
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now mind1-reindex.timer
```

Pull the checkouts before it runs, from the same timer or whatever already updates them.
The index is only as current as the working copies it read.

## Pinning an index

`mind1 pin` freezes the served index as a named snapshot for a repeatable evaluation. The
commands and the full guide are in [pinning.md](pinning.md).

The server needs a writable directory for the snapshots: `pins/` next to the index by
default, or wherever `--pins-dir` says. Under Compose this is already set up, with the
index volume still read-only and a separate `mind1-pins` volume mounted at `/pins`, so
pinning works out of the box and the server still cannot write the index. Change it only if
you want the snapshots somewhere specific, by repointing that volume.

On the systemd deployment the snapshots land under `$MIND1_HOME`, which is why the unit
above sets `ReadWritePaths` rather than `ReadOnlyPaths`.

Two environment variables set retention: `MIND1_RETAIN_PINNED_DAYS` (default 30) and
`MIND1_PIN_EXTEND_MAX_DAYS` (default 10, the most one extension may add). The pin routes
use the same token as `/mcp`.

## Connecting clients

Engineers need the URL and the token. For Claude Code:

```bash
claude mcp add --transport http mind1 http://<host>:8081/mcp --header "Authorization: Bearer <token>"
```

For clients configured by file:

```json
{
  "mcpServers": {
    "mind1": {
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

If you need per-user identity, replace `BearerAuth` in `mind1/server/http.py` with
your identity provider's middleware. Nothing else in the server knows how the caller was
authenticated.
