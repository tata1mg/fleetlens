"""Messaging (async) interface discovery — the queues/topics a service publishes to or
consumes from. Generic, library-level; no per-repo rules.

Two deterministic sources, combined:
  * Code sites — calls like `client.send_message(QueueUrl=…)`, `producer.produce(topic, …)`,
    `sqs.subscribe(…)` where the receiver looks like a messaging client or the module imports
    a messaging library. They prove the service does messaging and give the direction. A
    literal channel argument is an interface outright; a non-literal one is recorded as a
    `kind="event"` skipped site for the LLM gap-filler.
  * Config channels — channel-looking keys (`QUEUE_NAME`, `TOPIC_ARN`, `*_QUEUE`, …) in the
    repo's config files (`config*.json|yaml`, `.env*`, `settings*.py`), with the direction read
    from the key path's vocabulary (SUBSCRIBE/CONSUME vs PUBLISH/DISPATCH/TRIGGER). Only used
    when the service has messaging code sites, so a stray queue name in an unrelated repo is
    not an interface. Unknown direction -> skipped site.

Channel names are the cross-repo join key: the resolver links a publisher and a consumer of
the same channel with a `publishes_to` edge.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Optional

from ._pysrc import parse
from ._walk import iter_files
from .base import Interface, InterfaceAdapter, SkippedSite, names_in, snippet_of

PUBLISH = "PUBLISH"
CONSUME = "CONSUME"
# The config names a channel but nothing says which way it flows. Recording the coupling is
# more useful than discarding it: two services on the same channel ARE related, and saying
# "direction unknown" is honest where guessing would not be.
USES = "USES"

_PUBLISH_METHODS = {"send_message", "send_message_batch", "publish", "publish_to_sqs",
                    "publish_message", "produce", "put_record", "put_records", "basic_publish",
                    "xadd", "apply_async", "send_task", "emit", "sendmessage", "sendMessage",
                    "sendMessageBatch", "putRecord"}
_CONSUME_METHODS = {"receive_message", "receive_messages", "subscribe", "subscribe_forever",
                    "consume", "basic_consume", "xread", "xreadgroup", "poll", "receiveMessage",
                    "get_messages", "listen"}
_SETUP_METHODS = {"get_queue_url", "get_sqs_client", "create_queue", "get_queue_by_name",
                  "getQueueUrl", "queue_declare"}
_RECEIVER_HINTS = ("sqs", "sns", "queue", "topic", "kafka", "producer", "consumer",
                   "publisher", "subscriber", "pubsub", "broker", "channel", "stream",
                   "exchange", "celery", "rabbit", "nats", "servicebus", "messaging")
_MESSAGING_LIBS = ("boto3", "aiobotocore", "botocore", "kafka", "aiokafka", "confluent_kafka",
                   "pika", "aio_pika", "celery", "google.cloud.pubsub", "azure.servicebus",
                   "nats", "kombu", "faststream", "commonutils", "@aws-sdk/client-sqs",
                   "@aws-sdk/client-sns", "aws-sdk", "kafkajs", "amqplib", "bullmq", "sqs-consumer")
_CHANNEL_KWARGS = ("QueueUrl", "QueueName", "TopicArn", "TargetArn", "topic", "topics",
                   "queue", "queue_name", "queue_url", "routing_key", "exchange", "channel",
                   "stream", "StreamName", "name")

_SKIP = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", "dist", "build",
         "vendor", ".context", "tests", "test"}

# Declaring a messaging client as a dependency is evidence the service does messaging, in any
# language. Without this, config channels are only trusted where we have an AST adapter, so a
# Rails app with queues in config would contribute nothing.
_DEP_FILES = ("Gemfile", "Gemfile.lock", "package.json", "requirements.txt", "Pipfile",
              "pyproject.toml", "go.mod", "pom.xml", "build.gradle")
_DEP_LIBS = ("karafka", "shoryuken", "sidekiq", "bunny", "racecar", "sneakers", "aws-sdk-sqs",
             "aws-sdk-sns", "boto3", "aiobotocore", "kafka", "confluent", "pika", "celery",
             "kombu", "nats", "@aws-sdk/client-sqs", "@aws-sdk/client-sns", "kafkajs",
             "amqplib", "bullmq", "sqs-consumer")


def declares_messaging_dependency(repo: Path) -> str:
    """The manifest entry naming a messaging client, or "" if there is none."""
    for name in _DEP_FILES:
        f = Path(repo) / name
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
        for lib in _DEP_LIBS:
            if lib in text:
                return f"{name}:{lib}"
    return ""

# --- config channels ------------------------------------------------------
_CHANNEL_KEY = re.compile(r"(^|_)(QUEUE(_NAME|_URL|_ARN)?|TOPIC(_NAME|_ARN)?|STREAM(_NAME)?|"
                          r"EXCHANGE|ROUTING_KEY)$", re.I)
_CONSUME_WORDS = re.compile(r"(SUBSCRIBE|SUBSCRIPTION|CONSUME|CONSUMER|LISTEN|RECEIVE|INCOMING|INBOUND|WORKER)", re.I)
_PUBLISH_WORDS = re.compile(r"(PUBLISH|PRODUCE|PRODUCER|DISPATCH|TRIGGER|SEND|EMIT|OUTGOING|OUTBOUND|NOTIFY)", re.I)
_CONFIG_GLOBS = ("config*.json", "config*.yaml", "config*.yml", "settings*.json", "settings*.yaml",
                 "settings*.yml", ".env*", "*.env", "settings*.py", "config*.py")


def _iter_py(repo: Path):
    yield from iter_files(repo, (".py",))


def _imports_messaging(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name.split(".")[0] in _MESSAGING_LIBS or a.name in _MESSAGING_LIBS for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in _MESSAGING_LIBS or node.module in _MESSAGING_LIBS:
                return True
    return False


def _direction(method: str) -> Optional[str]:
    if method in _PUBLISH_METHODS:
        return PUBLISH
    if method in _CONSUME_METHODS:
        return CONSUME
    return None


def _channel_arg(call: ast.Call, method: str):
    """The argument that names the channel: a known kwarg, else arg0 for topic-first APIs."""
    for kw in call.keywords:
        if kw.arg in _CHANNEL_KWARGS:
            return kw.value
    if call.args and method in ("publish", "produce", "subscribe", "get_queue_url",
                                "get_queue_by_name", "queue_declare", "basic_publish", "xadd",
                                "xread", "emit", "send_task"):
        return call.args[0]
    return None


def _literal(node) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.strip():
        return node.value
    return None


#: Every method name that can produce a result below. A file whose text contains none of
#: them cannot yield an interface or a skipped site, because both are appended only inside
#: the branch that matches one. So this filter drops files without parsing them, and does
#: so exactly rather than heuristically: no candidate is lost.
_METHOD_PROBE = re.compile("|".join(
    re.escape(m) for m in sorted(_PUBLISH_METHODS | _CONSUME_METHODS | _SETUP_METHODS)))


def messaging_sites(repo: Path, skipped: Optional[list[SkippedSite]] = None) -> list[Interface]:
    """Python code sites. Returns literal-channel interfaces; records the rest in `skipped`."""
    repo = Path(repo)
    out: list[Interface] = []
    for p in _iter_py(repo):
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not _METHOD_PROBE.search(src):
            continue                      # cannot match below; not worth parsing
        tree = parse(src, p)
        if tree is None:
            continue
        rel = p.relative_to(repo).as_posix()
        lines = src.splitlines()
        # Deferred: it is a whole extra walk of the tree, and it is only consulted when a
        # candidate call is found whose receiver name gave nothing away.
        lib_cache: list = []

        def has_messaging_import(_tree=tree, _cache=lib_cache) -> bool:
            if not _cache:
                _cache.append(_imports_messaging(_tree))
            return _cache[0]

        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            method = node.func.attr
            if method not in _PUBLISH_METHODS | _CONSUME_METHODS | _SETUP_METHODS:
                continue
            recv = ast.get_source_segment(src, node.func.value) or ""
            looks = any(h in recv.lower() for h in _RECEIVER_HINTS) or (
                method not in ("publish", "send", "emit", "poll", "listen", "subscribe")
                and has_messaging_import())
            if not looks:
                continue
            direction = _direction(method)
            arg = _channel_arg(node, method)
            channel = _literal(arg) if arg is not None else None
            if channel and direction:
                out.append(Interface(method=direction, path=channel, type="event",
                                     handler=None, framework="messaging",
                                     evidence=[f"{rel}:{node.lineno}"]))
                continue
            if skipped is not None:
                expr = (ast.get_source_segment(src, arg) if arg is not None else None) or ""
                skipped.append(SkippedSite(
                    kind="event", reason="non-literal-channel" if arg is not None else "no-channel-arg",
                    file=rel, line=node.lineno, expr=expr or f"{recv}.{method}(...)",
                    snippet=snippet_of(lines, node.lineno, getattr(node, "end_lineno", node.lineno)),
                    names=(names_in(arg) if arg is not None else []) + names_in(node.func.value),
                    method=direction))
    return out


def messaging_sites_ts(repo: Path, skipped: Optional[list[SkippedSite]] = None) -> list[Interface]:
    """TypeScript code sites (`sqs.sendMessage({QueueUrl})`, `producer.send({topic})`,
    `consumer.subscribe({topic})`). Channel is read from a literal first arg only; object
    literals are recorded as skipped sites for the gap-filler."""
    from ._ts import iter_calls, iter_ts_files
    repo = Path(repo)
    out: list[Interface] = []
    for p in iter_ts_files(repo):
        rel = p.relative_to(repo).as_posix()
        lines = None
        for call in iter_calls(p):
            method = call.verb
            if method not in _PUBLISH_METHODS | _CONSUME_METHODS | _SETUP_METHODS:
                continue
            if not call.obj or not any(h in call.obj.lower() for h in _RECEIVER_HINTS):
                continue
            direction = _direction(method)
            if call.arg0 and direction and "{}" not in call.arg0:
                out.append(Interface(method=direction, path=call.arg0, type="event",
                                     handler=None, framework="messaging",
                                     evidence=[f"{rel}:{call.line}"]))
                continue
            if skipped is not None:
                if lines is None:
                    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
                skipped.append(SkippedSite(
                    kind="event", reason="non-literal-channel", file=rel, line=call.line,
                    expr=call.arg0_expr or (f"`{call.arg0}`" if call.arg0 else f"{call.obj}.{method}(...)"),
                    snippet=snippet_of(lines, call.line, call.end_line),
                    names=call.arg0_names + [call.obj], method=direction))
    return out


def _flatten(obj, prefix: str = ""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _flatten(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _flatten(v, f"{prefix}[{i}]")
    else:
        yield prefix, obj


def _load_config(p: Path):
    text = p.read_text(encoding="utf-8", errors="replace")
    if p.suffix == ".json":
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None
    if p.suffix in (".yaml", ".yml"):
        try:
            import yaml  # optional dependency
            return yaml.safe_load(text)
        except Exception:  # noqa: BLE001
            return None
    # .env / settings.py: KEY = "value" / KEY=value lines
    out = {}
    for line in text.splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*[=:]\s*['\"]?([^'\"#\n]+?)['\"]?\s*(#.*)?$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out or None


def config_channels(repo: Path) -> list[dict]:
    """[{key, value, direction|None, file}] for channel-looking keys in config files."""
    repo = Path(repo)
    seen = set()
    out = []
    for pattern in _CONFIG_GLOBS:
        for p in sorted(repo.rglob(pattern)):
            rel_parts = p.relative_to(repo).parts
            if any(part in _SKIP for part in rel_parts) or not p.is_file():
                continue
            # config lives near the root; a `settings.py` deep in app code is a module
            if len(rel_parts) > 3 or (p.suffix == ".py" and len(rel_parts) > 2):
                continue
            data = _load_config(p)
            if not isinstance(data, (dict, list)):
                continue
            for key, value in _flatten(data):
                leaf = key.rsplit(".", 1)[-1]
                if not isinstance(value, str) or not value.strip() or not _CHANNEL_KEY.search(leaf):
                    continue
                if value.startswith(("http://", "https://")) and "queue" not in leaf.lower():
                    continue
                path_words = key.rsplit(".", 1)[0] if "." in key else ""
                direction = None
                if _CONSUME_WORDS.search(path_words) and not _PUBLISH_WORDS.search(path_words):
                    direction = CONSUME
                elif _PUBLISH_WORDS.search(path_words) and not _CONSUME_WORDS.search(path_words):
                    direction = PUBLISH
                elif _CONSUME_WORDS.search(path_words) and _PUBLISH_WORDS.search(path_words):
                    # both present: the innermost (closest to the leaf) wins
                    c = max(m.start() for m in _CONSUME_WORDS.finditer(path_words))
                    pb = max(m.start() for m in _PUBLISH_WORDS.finditer(path_words))
                    direction = CONSUME if c > pb else PUBLISH
                rec = (key, value.strip())
                if rec in seen:
                    continue
                seen.add(rec)
                out.append({"key": key, "value": value.strip(), "direction": direction,
                            "file": p.relative_to(repo).as_posix()})
    return out


class MessagingAdapter(InterfaceAdapter):
    name = "messaging"

    def __init__(self) -> None:
        # Keyed by repo path. One entry per repo indexed in this process.
        self._cache: dict = {}

    # The registry asks `applies` before `discover`, and for this adapter the honest answer
    # to "does messaging appear here" is the scan itself. Running it twice made this adapter
    # alone three quarters of all interface-discovery time. The scan is pure, so the first
    # result answers both questions.
    def _scan(self, repo: Path) -> tuple:
        key = str(Path(repo).resolve())
        hit = self._cache.get(key)
        if hit is None:
            sites: list[SkippedSite] = []
            out = messaging_sites(Path(repo), sites) + messaging_sites_ts(Path(repo), sites)
            hit = self._cache[key] = (out, sites)
        return hit

    def applies(self, repo: Path) -> bool:
        out, sites = self._scan(repo)
        return bool(out or sites or declares_messaging_dependency(Path(repo)))

    def discover(self, repo: Path, skipped: Optional[list[SkippedSite]] = None) -> list[Interface]:
        repo = Path(repo)
        scanned, sites = self._scan(repo)
        out = list(scanned)
        if skipped is not None:
            skipped.extend(sites)
        dep = declares_messaging_dependency(repo)
        if not out and not sites and not dep:
            return out  # no messaging code or dependency -> config queue names are not ours
        seen = {(i.method, i.path) for i in out}
        for ch in config_channels(repo):
            direction = ch["direction"] or USES
            if (direction, ch["value"]) in seen:
                continue
            seen.add((direction, ch["value"]))
            out.append(Interface(method=direction, path=ch["value"], type="event",
                                 handler=None, summary=None, framework="config",
                                 evidence=[f"{ch['file']}:{ch['key']}"]))
        return out
