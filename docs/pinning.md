# Pinning an index

The index is a snapshot of your repositories, and on a shared server it is rebuilt on a
schedule. That is what you want day to day: the answers keep up with the code.

It is the wrong thing when you are measuring something. If the index changes between two
runs of an agent or a harness, a difference in the result cannot be attributed to the
change you made. Pinning freezes a copy of the index and serves it at its own URL, so you
can come back to the same context tomorrow and get the same answers.

## Before you start

Two things have to be true of the server, not of your machine:

- **SQLite 3.27 or newer**, because the snapshot is taken with `VACUUM INTO`.
- **An index built by 0.2.0 or later.** Earlier indexes carry no build id, and there is
  nothing to name a pin after, so `fl pin create` returns 409. Re-index to fix it.

## The short version

```bash
export FLEETLENS_URL=http://fleetlens.internal:8081
export FLEETLENS_TOKEN=...                         # the team's shared token

fl pin create --as eval-q4 --reason "baseline for the Q4 agent run"
# pinned build 20261021T071755Z as "eval-q4", expires 2026-11-20
```

Then point your MCP client at the pin instead of the live server:

```
http://fleetlens.internal:8081/mcp                 # always the newest index
http://fleetlens.internal:8081/pins/eval-q4/mcp    # your snapshot, until it expires
```

Same tools, same token, same responses. Nothing else about your setup changes.

## How it works

A fleetlens index is one SQLite file, so a pin is a copy of that file with a name and an
expiry date.

1. **`fl pin create` snapshots the index the server is serving right now.** The copy is
   taken with SQLite's `VACUUM INTO`, which is consistent even if a rebuild is running, so
   you never get a half-written pin.
2. **The copy never changes.** Later rebuilds replace the live index and leave your pin
   alone.
3. **The same server serves it**, at `/pins/<alias>/mcp`, from the snapshot rather than the
   live file.
4. **It is deleted when it expires.** The server sweeps expired pins at startup and hourly.

You can only pin the index that is current when you ask. There is no way to pin a build
that has already been replaced, because unpinned builds are not kept: a busy deployment
produces dozens a day and storing them all is not affordable.

## Commands

All four talk to the server over HTTP. They do not touch your local machine's index and
they do not need one.

```bash
fl pin create --as <alias> [--reason "..."] [--owner "..."]
fl pin list
fl pin extend <alias> --days N
fl pin delete <alias>            # fl unpin <alias> does the same thing
```

`fl pin list` shows the alias, the build it captured, who made it, why, and when it goes:

```
ALIAS       BUILD             OWNER  REASON                         EXPIRES
eval-q4     20261021T083313Z  priya  baseline for the Q4 agent run  2026-11-20
harness-ab  20261019T140302Z  sam    harness A/B                    2026-11-18
```

### Aliases

Lower-case letters, digits, `.`, `_` and `-`, starting with a letter or digit, up to 63
characters. The alias becomes part of a URL and a filename, so anything else is rejected.

Pick a name you will recognise in three weeks. `eval-q4` is better than `test2`.

## Connecting the client

In order of precedence:

1. `--url` and `--token` on the command line
2. `FLEETLENS_URL` and `FLEETLENS_TOKEN` in the environment
3. `url` and `token` in `~/.config/fleetlens/config.toml`, or under `$XDG_CONFIG_HOME`

```toml
# ~/.config/fleetlens/config.toml
url = "http://fleetlens.internal:8081"
token = "..."
```

`FLEETLENS_TOKEN` is the same shared token your MCP client already sends, so if fleetlens
is configured in your editor you already have it.

### You do not have to install anything

`fl pin` is a convenience over plain HTTP. Everything it does works with curl, which is
useful from CI or from a machine where you do not want a Python environment:

```bash
curl -X POST "$FLEETLENS_URL/pins" \
     -H "Authorization: Bearer $FLEETLENS_TOKEN" \
     -d '{"alias":"eval-q4","reason":"baseline for the Q4 agent run"}'

curl -H "Authorization: Bearer $FLEETLENS_TOKEN" "$FLEETLENS_URL/pins"
curl -X PATCH  "$FLEETLENS_URL/pins/eval-q4" -H "Authorization: Bearer $FLEETLENS_TOKEN" -d '{"days":5}'
curl -X DELETE "$FLEETLENS_URL/pins/eval-q4" -H "Authorization: Bearer $FLEETLENS_TOKEN"
```

If you do install the CLI, the base install is enough. The pin commands need none of the
indexing toolchain, so no extras:

```bash
pip install "fleetlens @ git+https://github.com/tata1mg/fleetlens.git@v0.3.0#subdirectory=packages/fleetlens"
```

## Recording what you measured against

A pinned URL is only half of a reproducible result. The other half is writing down which
index produced it, so the number still means something after the pin has gone.

`get_index_info` tells you both:

```json
{
  "build_id": "20261021T071755Z",
  "serving": { "pin": "eval-q4", "live": false },
  "repositories": { "orders": "a1b2c3d...", "billing": "e4f5a6b..." }
}
```

- **`build_id`** names the build.
- **`serving`** says whether you were on a pin or on the live index. Worth asserting in a
  harness: a run that silently used the live index is not comparable with the others.
- **`repositories`** gives the git commit each repository was at, so someone can reconstruct
  what the index described even without the file.

Log all three with your results.

## Expiry

The server decides how long a pin lives, which is why `fl pin create` takes no duration. An
operator sets the policy once and nobody can opt out of it by asking for ten years.

| Setting | Default | Meaning |
| --- | --- | --- |
| `FLEETLENS_RETAIN_PINNED_DAYS` | 30 | How long a new pin lives |
| `FLEETLENS_PIN_EXTEND_MAX_DAYS` | 10 | The most one `extend` may add |

`fl pin extend` pushes the expiry out, by up to the cap each time:

```
$ fl pin extend eval-q4 --days 5
"eval-q4" now expires 2026-11-25

$ fl pin extend eval-q4 --days 99
fl pin: days must be a whole number from 1 to 10
```

You can extend more than once, so a long-running experiment is fine; you just have to come
back and say so.

A pin that is being served when it expires is not deleted mid-request. The sweep skips it
and removes it once nothing is using it.

## Things worth knowing

**Delete pins you are done with.** A pin is a full copy of the index, which on a real fleet
is hundreds of megabytes. There is no per-pin quota, so the disk is a shared resource and
the team's manners are what protect it.

**Anyone can delete anyone's pin.** The server uses one shared token, so `--owner` is a
label that makes `fl pin list` readable, not a permission. Name your pins clearly and check
with people before removing theirs.

**A handful of pins can be served at once.** Each open pin holds its enrichment vectors in
memory, so the server caches a few and reopens the rest on demand, controlled by
`FLEETLENS_MAX_OPEN_PINS`. The first semantic search against a pin that has just been opened
takes a moment longer; after that it is as fast as the live index.

**An alias that does not exist returns 404**, never a quiet fall back to the live index.
Getting the live index when you asked for a pin is the exact failure pinning exists to
prevent, so it fails loudly instead.

## Operator notes

- Snapshots live in `--pins-dir`, which defaults to a `pins/` directory next to `--db` and
  must be writable. Under Docker the default `/data` mount is read only, so mount a writable
  volume and point `--pins-dir` at it.
- The server refuses to start if `--pins-dir` contains the live index, because the sweep
  that removes abandoned snapshots would otherwise be pointed at it.
- Pin management shares the server's bearer token. Under `--insecure` it is unauthenticated,
  and the server logs a warning saying so.
- Expired pins are swept at startup, hourly, and whenever pins are listed.
