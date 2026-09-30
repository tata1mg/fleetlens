"""Messaging adapter (code sites + config channels), async resolver edges, event gap-fill."""
from __future__ import annotations

import json

from fleetlens.adapters.messaging import (
    MessagingAdapter,
    config_channels,
    messaging_sites,
)
from fleetlens.enrich.gaps import fill_gaps
from fleetlens.resolve import resolve
from fleetlens.store.models import KnowledgeObject
from fleetlens.store.sqlite import SqliteStore


def _write(root, name, content):
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content if isinstance(content, str) else json.dumps(content, indent=1))


def test_code_sites_literal_and_skipped(tmp_path):
    _write(tmp_path, "app/pub.py", '''
import boto3
sqs = boto3.client("sqs")

def go(url, d):
    sqs.send_message(QueueUrl="orders-created", MessageBody="x")   # literal -> interface
    sqs.send_message(QueueUrl=url, MessageBody="x")                 # non-literal -> skipped
    producer.produce("audit-topic", b"x")                            # receiver hint -> interface
    d.get("k")                                                       # not messaging
''')
    skipped = []
    ifaces = messaging_sites(tmp_path, skipped)
    assert {(i.method, i.path) for i in ifaces} == {("PUBLISH", "orders-created"), ("PUBLISH", "audit-topic")}
    assert all(i.type == "event" and i.id.startswith("publish-") for i in ifaces)
    assert [(s.kind, s.reason, s.method, s.expr) for s in skipped] == [("event", "non-literal-channel", "PUBLISH", "url")]


def test_config_channels_direction_from_key_path(tmp_path):
    _write(tmp_path, "config_template.json", {
        "TRIGGER": {"HIGH": {"QUEUE_NAME": "q-high"}},
        "SUBSCRIBE_TO": {"LOW": {"QUEUE_NAME": "q-low"}},
        "SQS": {"PUBLISH": {"LOG": {"QUEUE_NAME": "q-log"}}, "SUBSCRIBE": {"SMS": {"QUEUE_NAME": "q-sms"}}},
        "MISC": {"QUEUE_NAME": "q-unknown", "URL": "http://x", "TIMEOUT": "5"},
    })
    _write(tmp_path, "app/services/settings.py", 'providers_for_channel = "email"\n')  # a module, not config
    chans = {c["value"]: c["direction"] for c in config_channels(tmp_path)}
    assert chans == {"q-high": "PUBLISH", "q-low": "CONSUME", "q-log": "PUBLISH",
                     "q-sms": "CONSUME", "q-unknown": None}


def test_adapter_uses_config_only_when_code_does_messaging(tmp_path):
    _write(tmp_path, "config.json", {"DISPATCH": {"QUEUE_NAME": "q-out"}, "MISC": {"QUEUE_NAME": "q-?"}})
    assert not MessagingAdapter().applies(tmp_path)          # queue names alone are not evidence
    assert MessagingAdapter().discover(tmp_path, []) == []

    _write(tmp_path, "app/w.py", "async def f(self, m):\n    await self.sqs_manager.publish_to_sqs(payload=m)\n")
    assert MessagingAdapter().applies(tmp_path)
    skipped = []
    ifaces = MessagingAdapter().discover(tmp_path, skipped)
    got = {(i.method, i.path) for i in ifaces}
    # DISPATCH.* gives a direction; MISC.* does not, so it is recorded as USES rather than
    # discarded — two services on one channel are related even when the flow is unknown.
    assert got == {("PUBLISH", "q-out"), ("USES", "q-?")}
    assert {s.reason for s in skipped} == {"no-channel-arg"}


def _svc(slug):
    return KnowledgeObject("service", slug, slug, None, "unknown", "static", "index", None, None, {})


def _event(slug, direction, channel, framework="config"):
    return KnowledgeObject("interface", f"{slug}:{direction.lower()}-{channel}", f"{direction} {channel}",
                           None, "unknown", "static", "adapter", None, None,
                           {"type": "event", "method": direction, "path": channel, "framework": framework})


def test_resolver_joins_publishers_to_consumers_on_channel():
    s = SqliteStore(":memory:")
    for slug in ("gw", "core", "worker"):
        s.upsert_object(_svc(slug))
    s.upsert_object(_event("gw", "PUBLISH", "q-high"))
    s.upsert_object(_event("gw", "PUBLISH", "q-low"))
    s.upsert_object(_event("core", "CONSUME", "q-high"))
    s.upsert_object(_event("core", "CONSUME", "q-low"))
    s.upsert_object(_event("core", "PUBLISH", "q-email", framework="messaging"))
    s.upsert_object(_event("worker", "CONSUME", "q-email", framework="messaging"))
    s.upsert_object(_event("worker", "PUBLISH", "q-self"))
    s.upsert_object(_event("worker", "CONSUME", "q-self"))   # own work queue: no self edge
    s.commit()
    r = resolve(s, s, s)
    assert r["async_edges"] == 2
    edges = {(e.from_id, e.to_id): e.metadata for e in s.edges_of("service:core", "both")
             if e.relationship == "publishes_to"}
    assert edges[("service:gw", "service:core")] == {"channels": ["q-high", "q-low"], "confidence": "config"}
    assert edges[("service:core", "service:worker")] == {"channels": ["q-email"], "confidence": "static"}
    assert not [e for e in s.edges_of("service:worker", "both") if e.from_id == e.to_id]


class ScriptedLLM:
    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def complete(self, prompt, *, system=None, max_tokens=64):
        self.prompts.append(prompt)
        return self.replies.pop(0)


def test_event_gap_fill_grounds_on_config_key_or_literal(tmp_path):
    _write(tmp_path, "config.json", {"SQS": {"SUBSCRIBE": {"SMS": {"QUEUE_NAME": "stag-sms"}}}})
    _write(tmp_path, "app/w.py", "async def f(self):\n    await self.sqs_client.get_sqs_client(queue_name=self.queue_name)\n")
    skipped = []
    messaging_sites(tmp_path, skipped)
    from dataclasses import asdict
    s = SqliteStore(":memory:")
    s.upsert_object(KnowledgeObject("service", "svc", "svc", None, "unknown", "static", "index", None, None,
                                    {"root": str(tmp_path), "skipped": [asdict(x) for x in skipped]}))
    s.commit()
    assert skipped[0].method is None  # setup call: direction unknown, LLM decides

    # config key -> value from the repo's config file
    llm = ScriptedLLM(['{"resolved": true, "direction": "CONSUME", "config_key": "SQS.SUBSCRIBE.SMS.QUEUE_NAME"}'])
    r = fill_gaps(s, llm)
    assert r["resolved"] == 1 and r["interfaces"] == 1
    obj = s.get("interface:svc:consume-stag-sms")
    assert obj and obj.source == "llm" and obj.payload["type"] == "event"
    assert "Trace it to the concrete queue/topic name" in llm.prompts[0]

    # a made-up literal is rejected
    s.upsert_object(KnowledgeObject("service", "svc", "svc", None, "unknown", "static", "index", None, None,
                                    {"root": str(tmp_path), "skipped": [asdict(x) for x in skipped]}))
    r = fill_gaps(s, ScriptedLLM(['{"resolved": true, "direction": "CONSUME", "channel": "prod-sms-queue"}']))
    assert r["rejected"] == 1



def test_direction_unknown_channels_give_a_shares_channel_edge():
    """Most real configs name a queue without saying which way it flows."""
    s = SqliteStore(":memory:")
    for slug in ("dexter", "merch"):
        s.upsert_object(_svc(slug))
    s.upsert_object(_event("dexter", "USES", "diagnostics-test_inventories"))
    s.upsert_object(_event("merch", "USES", "diagnostics-test_inventories"))
    s.commit()
    r = resolve(s, s, s)
    assert r["async_edges"] == 0 and r["shared_channel_edges"] == 1
    e = [x for x in s.edges_of("service:dexter", "both") if x.relationship == "shares_channel"]
    assert e and e[0].metadata["confidence"] == "direction-unknown"


def test_a_known_publisher_directs_an_unknown_end():
    s = SqliteStore(":memory:")
    for slug in ("core", "worker"):
        s.upsert_object(_svc(slug))
    s.upsert_object(_event("core", "PUBLISH", "jobs"))
    s.upsert_object(_event("worker", "USES", "jobs"))
    s.commit()
    r = resolve(s, s, s)
    assert r["async_edges"] == 1 and r["shared_channel_edges"] == 0


def test_files_without_a_candidate_call_are_not_parsed(tmp_path, monkeypatch):
    """The scan drops files by text before parsing them. That is only safe because every
    result is appended inside the branch matching one of those method names, so a file
    without one cannot contribute. This pins that the filter stays exact."""
    import ast as ast_mod

    from fleetlens.adapters import messaging as m

    (tmp_path / "noise.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "real.py").write_text(
        "import boto3\n"
        "sqs = boto3.client('sqs')\n"
        "sqs.send_message(QueueUrl='orders-queue', MessageBody='{}')\n")

    parsed: list = []
    real_parse = ast_mod.parse
    monkeypatch.setattr(m.ast, "parse", lambda src, *a, **kw: (parsed.append(src), real_parse(src))[1])

    found = m.messaging_sites(tmp_path, [])
    assert any("orders-queue" in (i.path or "") for i in found)
    assert len(parsed) == 1                      # noise.py never reached the parser


def test_applies_and_discover_scan_only_once(tmp_path):
    """`applies` used to re-run the whole scan that `discover` then repeated, which made
    this adapter three quarters of all interface-discovery time."""
    from fleetlens.adapters.messaging import MessagingAdapter

    (tmp_path / "svc.py").write_text(
        "import boto3\n"
        "sqs = boto3.client('sqs')\n"
        "sqs.send_message(QueueUrl='orders-queue')\n")

    adapter = MessagingAdapter()
    calls = {"n": 0}
    real = adapter._scan

    def counted(repo):
        calls["n"] += 1
        return real(repo)

    adapter._scan = counted
    assert adapter.applies(tmp_path)
    got = adapter.discover(tmp_path, [])
    assert calls["n"] == 2                       # two questions asked
    assert len(adapter._cache) == 1              # one scan performed
    assert any("orders-queue" in (i.path or "") for i in got)
