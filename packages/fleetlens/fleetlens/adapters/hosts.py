"""Service addresses declared in configuration — the symmetric case to `config_channels`.

Twelve-factor apps keep the address of every service they call in config or env, under
keys like `ORDERS_HOST`, `PAYMENTS_BASE_URL`, `AUTH_SERVICE_ENDPOINT`. That convention is
not one organisation's habit: Kubernetes service DNS, docker-compose service names, Consul
and `.env` files all express "where service X lives" this way. Extracting those bindings is
therefore generic, and is exactly what the messaging adapter already does for queue names.

What is NOT generic is deciding WHICH service a given host string denotes. `orders-svc`,
`orders.prod.svc.cluster.local` and `internal-lb-7.example.com` may all mean the `orders`
service, and only the last one needs local knowledge. So resolution is pluggable:

    1. ManifestResolver      — explicit `hosts:` mappings in fleetlens.yaml (zero code)
    2. KubernetesDNSResolver — <service>.<ns>.svc.cluster.local and friends
    3. SlugMatchResolver     — the host names a fleet service (the common default)

Resolvers are tried in order and the first hit wins. Register your own by appending to
`HOST_RESOLVERS`; nothing in the core needs to change.

A config-declared address is evidence of INTENT to call, not proof of a call — a config may
list a host nobody uses. Edges from this source are therefore emitted at `config`
confidence, and upgraded when an observed request path corroborates them.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from ._walk import ALWAYS_EXCLUDE, _excluded, load_contextignore

_SKIP = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", "dist", "build",
         "vendor", ".context", "tests", "test"}
_CONFIG_GLOBS = ("config*.json", "config*.yaml", "config*.yml", "settings*.json",
                 "settings*.yaml", "settings*.yml", ".env*", "*.env")

# A key that names somewhere to send requests.
_HOST_KEY = re.compile(r"(^|_)(HOST|HOSTNAME|URL|URI|ENDPOINT|BASE_URL|BASE_URI|ADDR|ADDRESS|SERVER)$", re.I)
# Public suffixes we will never treat as a fleet service.
_THIRD_PARTY = re.compile(
    r"(amazonaws\.com|googleapis\.com|google\.com|stripe\.com|twilio\.com|sentry\.io|"
    r"datadoghq\.com|slack\.com|github\.com|facebook\.com|cloudfront\.net|akamai)", re.I)


# `scheme://user:password@host` — the standard way a connection string carries a secret,
# and the reason a DATABASE_URL or AMQP_URL cannot be stored as written.
_USERINFO = re.compile(r"(?P<scheme>[a-z][a-z0-9+.\-]*://)[^/@\s]*@", re.I)
# A value that is mostly opaque characters is a token, whatever the key is called.
_OPAQUE = re.compile(r"^[A-Za-z0-9+/_\-]{24,}={0,2}$")


def redact(value: str) -> str:
    """A config value safe to keep.

    fleetlens reads config to learn which services talk to which, and the useful part of
    `postgres://admin:hunter2@db.internal:5432/orders` is `db.internal:5432`. The credential
    in the middle is incidental, and an index is meant to be shared: the deployment guide
    says to copy the database file around, and the benchmark publishes what it contains. So
    the value is stored with the secret removed rather than trusting that no one looks.
    """
    if not value:
        return value
    cleaned = _USERINFO.sub(lambda m: m.group("scheme"), value)
    if _OPAQUE.match(cleaned.strip()):
        return "[redacted]"
    return cleaned


@dataclass
class HostBinding:
    """One `<key> = <address>` pair found in a repo's configuration."""
    key: str            # dotted key path, e.g. "SERVICES.ORDERS.HOST"
    value: str          # address with any credential removed, e.g. "http://orders-svc:8080"
    file: str = ""      # repo-relative config file
    host: str = ""      # bare hostname
    port: str = ""      # port, when the address carries one

    def __post_init__(self):
        # Parse host and port from the value as written, then keep only the redacted form.
        # Doing it in this order means a credentialled URL still resolves to the right
        # service while the credential never reaches the store.
        if not self.host:
            self.host = bare_host(self.value)
        if not self.port:
            self.port = bare_port(self.value)
        self.value = redact(self.value)


@dataclass
class ResolveContext:
    """Fleet-level facts a resolver may consult.

    Resolvers get the whole binding (key, value, host, port) rather than a bare hostname,
    because the signal is not always in the host: a dev config says `localhost:9402` and the
    PORT is what identifies the service, while `NOTIFICATION_SERVICE.HOST` carries it in the
    key.
    """
    slugs: set          # service slugs in the fleet
    declared: dict = field(default_factory=dict)   # manifest `hosts:` mappings
    ports: dict = field(default_factory=dict)      # "9402" -> slug, from each service's config
    aliases: dict = field(default_factory=dict)    # "notification_core" -> slug


_LOOPBACK = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"}


def bare_port(value: str) -> str:
    m = re.search(r":(\d{2,5})(?:[/?#]|$)", (value or "").strip())
    return m.group(1) if m else ""


def bare_host(value: str) -> str:
    """`http://orders-svc:8080/v1` -> `orders-svc`."""
    v = (value or "").strip()
    v = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", v, flags=re.I)
    v = v.split("/")[0].split("?")[0]
    v = v.split("@")[-1]
    return v.split(":")[0].strip().lower()


def slugish(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


# --- resolvers -------------------------------------------------------------
@runtime_checkable
class HostResolver(Protocol):
    name: str
    def resolve(self, binding: "HostBinding", ctx: "ResolveContext") -> Optional[str]: ...


class ManifestResolver:
    """Explicit mappings from a repo's `fleetlens.yaml`:

        hosts:
          internal-lb-7.example.com: orders
          "*.payments.internal": payments

    The escape hatch for addresses no heuristic could resolve, and the reason a team never
    has to patch fleetlens to describe their own naming.
    """
    name = "manifest"

    def resolve(self, binding, ctx):
        host = binding.host
        if host in ctx.declared:
            return ctx.declared[host]
        for pattern, svc in ctx.declared.items():
            if "*" in pattern and re.fullmatch(re.escape(pattern).replace(r"\*", ".*"), host):
                return svc
        return None


class KubernetesDNSResolver:
    """`orders.default.svc.cluster.local` / `orders.default` -> `orders`."""
    name = "k8s-dns"

    def resolve(self, binding, ctx):
        host = binding.host
        if ".svc" not in host and not host.endswith(".cluster.local"):
            return None
        return _match_slug(host.split(".")[0], ctx.slugs)


class SlugMatchResolver:
    """The host names a service in the fleet: `orders-svc`, `orders_service:8080`, `orders`.

    Deliberately conservative — never matches a known third-party domain, and requires the
    slug to appear as whole tokens, so `orders` does not match `reorders-legacy`.
    """
    name = "slug-match"

    def resolve(self, binding, ctx):
        if _THIRD_PARTY.search(binding.host):
            return None
        return _match_slug(binding.host, ctx.slugs)


class PortResolver:
    """`http://localhost:9402` -> the service whose own config declares `PORT: 9402`.

    Development and docker-compose configurations address peers on loopback, so the hostname
    carries no information but the port identifies the service exactly. Only applied to
    loopback addresses: on a real hostname the host itself is the better signal, and a port
    like 8080 is shared by everything.
    """
    name = "port"

    def resolve(self, binding, ctx):
        if binding.host not in _LOOPBACK or not binding.port:
            return None
        return ctx.ports.get(binding.port)


class KeyNameResolver:
    """`NOTIFICATION_SERVICE.HOST = localhost:9402` -> the service called that.

    When the value is uninformative the KEY often names the target. Matched against both
    fleet slugs and the names services declare for themselves in their own config.
    """
    name = "key-name"

    def resolve(self, binding, ctx):
        if binding.host and binding.host not in _LOOPBACK:
            return None          # a real host is better evidence than a key name
        for seg in reversed(binding.key.split(".")):
            token = re.sub(r"(?i)_?(HOST|HOSTNAME|URL|URI|ENDPOINT|BASE_URL|BASE_URI|ADDR|ADDRESS|SERVER)$", "", seg)
            if not token:
                continue
            hit = ctx.aliases.get(slugish(token)) or _match_slug(token, ctx.slugs)
            if hit:
                return hit
        return None


def _match_slug(host: str, slugs) -> Optional[str]:
    """The slug must appear as a CONTIGUOUS run of tokens starting at the first one.

    Anchoring matters for precision. `USER_GENERATED_CONTENT_SERVICE` contains the tokens
    of `content_service` contiguously, but it names a different service — the tokens are not
    at the start, so it is rejected. `orders-svc` and `orders-service:8080` still match,
    because a service's address begins with its name.
    """
    tokens = [t for t in re.split(r"[^a-z0-9]+", (host or "").lower()) if t]
    if not tokens:
        return None
    best = None
    for slug in slugs:
        s = slugish(slug)
        parts = [p for p in s.split("-") if p]
        if not parts or len(parts) > len(tokens):
            continue
        if tokens[:len(parts)] == parts and (best is None or len(s) > len(slugish(best))):
            best = slug          # prefer the most specific match
    return best


# Tried in order; first hit wins. Append your own — the core never changes.
HOST_RESOLVERS: list = [
    ManifestResolver(), KubernetesDNSResolver(), SlugMatchResolver(),
    PortResolver(), KeyNameResolver(),
]


def resolve_host(binding, ctx) -> Optional[str]:
    if isinstance(binding, str):                       # convenience for callers/tests
        binding = HostBinding(key="", value=binding)
    for r in HOST_RESOLVERS:
        hit = r.resolve(binding, ctx)
        if hit:
            return hit
    return None


# --- extraction ------------------------------------------------------------
def _flatten(obj, prefix: str = ""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _flatten(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _flatten(v, f"{prefix}[{i}]")
    else:
        yield prefix, obj


def _load(p: Path):
    text = p.read_text(encoding="utf-8", errors="replace")
    if p.suffix == ".json":
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None
    if p.suffix in (".yaml", ".yml"):
        try:
            import yaml
            return yaml.safe_load(text)
        except Exception:  # noqa: BLE001
            return None
    out = {}
    for line in text.splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*[=:]\s*['\"]?([^'\"#\n]+?)['\"]?\s*(#.*)?$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out or None


def _config_files(repo: Path, max_parts: int) -> list[Path]:
    """Config files lying within `max_parts` path segments of the repo root.

    Walks once, pruning skipped directories as it descends, rather than globbing the whole
    tree per pattern and discarding the misses afterwards. `rglob` cannot be pruned, so on
    a repo with a virtualenv or node_modules in the working tree the old approach visited
    thousands of files, once for each of eight patterns, to find a handful of configs. That
    cost scaled with what happened to be checked out rather than with the project.
    """
    out: list[Path] = []
    # A repo can exclude a config file from being read at all. Config is the one place
    # fleetlens deliberately reads files that may hold secrets, so the escape hatch belongs
    # here more than anywhere else.
    patterns = load_contextignore(repo)

    def walk(d: Path, depth: int) -> None:
        try:
            entries = sorted(d.iterdir())
        except OSError:
            return
        for p in entries:
            rel = p.relative_to(repo).as_posix()
            if p.is_dir():
                if (p.name not in _SKIP and depth + 1 < max_parts
                        and not _excluded(rel, p.name, patterns)):
                    walk(p, depth + 1)
            elif any(fnmatch(p.name, g) for g in _CONFIG_GLOBS):
                # ALWAYS_EXCLUDE is not applied to config: reading `.env` for `ORDERS_HOST`
                # is the point, and values are redacted on the way into HostBinding. A repo
                # that disagrees says so in .contextignore.
                if not _excluded(rel, p.name, tuple(patterns[len(ALWAYS_EXCLUDE):])):
                    out.append(p)

    walk(repo, 0)
    return out


def config_hosts(repo: Path) -> list[HostBinding]:
    """Every service address declared in this repo's configuration."""
    repo = Path(repo)
    seen, out = set(), []
    for p in _config_files(repo, 3):          # config lives near the root
        data = _load(p)
        if not isinstance(data, (dict, list)):
            continue
        for key, value in _flatten(data):
            leaf = key.rsplit(".", 1)[-1]
            if not isinstance(value, str) or not value.strip():
                continue
            if not _HOST_KEY.search(leaf):
                continue
            host = bare_host(value)
            if not host:
                continue
            # Loopback addresses are KEPT: PortResolver and KeyNameResolver exist to
            # resolve exactly those (dev/compose configs address peers on localhost).
            # A bare top-level HOST with no port is this service's own bind address.
            if host in _LOOPBACK and "." not in key and not bare_port(value):
                continue
            rec = (key, host)
            if rec in seen:
                continue
            seen.add(rec)
            out.append(HostBinding(key=key, value=value.strip(),
                                   file=p.relative_to(repo).as_posix()))
    return out


_SELF_PORT_KEY = re.compile(r"^(PORT|SERVER_PORT|HTTP_PORT|APP_PORT|LISTEN_PORT)$", re.I)
_SELF_NAME_KEY = re.compile(r"^(NAME|APP_NAME|SERVICE_NAME|PROJECT_NAME)$", re.I)


def service_identity(repo: Path) -> dict:
    """What a service says about ITSELF: the port it listens on and the name it calls itself.

    Peers address it by those, so they are the join keys for `PortResolver` and
    `KeyNameResolver`. Only shallow keys count — a nested `REDIS.PORT` is a dependency's
    port, not this service's.
    """
    repo = Path(repo)
    out: dict = {"port": "", "name": ""}
    for f in _config_files(repo, 2):
        data = _load(f)
        if not isinstance(data, dict):
            continue
        for k, v in data.items():
            if not isinstance(v, (str, int)):
                continue
            if not out["port"] and _SELF_PORT_KEY.match(str(k)):
                out["port"] = str(v).strip()
            if not out["name"] and _SELF_NAME_KEY.match(str(k)):
                out["name"] = str(v).strip()
    return out
