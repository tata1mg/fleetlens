"""What the indexer is allowed to read, and what it keeps from what it reads.

fleetlens reads a private codebase and writes a database the deployment guide tells people
to copy around. These pin both halves of that: which files are opened at all, and what
survives into the store.
"""
from __future__ import annotations

from fleetlens.adapters._walk import ALWAYS_EXCLUDE, iter_files, load_contextignore
from fleetlens.adapters.hosts import _config_files, config_hosts, redact


def test_credentials_in_a_connection_string_are_not_stored(tmp_path):
    """The reason this matters: a DATABASE_URL carries its password in the middle of the
    address, and the address is the part fleetlens wants."""
    (tmp_path / ".env").write_text(
        "DATABASE_URL=postgres://admin:SuperSecret123@db.internal:5432/orders\n"
        "REDIS_URL=redis://:AnotherSecret@cache.internal:6379/0\n"
        "ORDERS_SERVICE_HOST=orders-svc\n")

    by_key = {b.key: b for b in config_hosts(tmp_path)}

    assert "SuperSecret123" not in by_key["DATABASE_URL"].value
    assert "AnotherSecret" not in by_key["REDIS_URL"].value
    # the useful part survives, or redaction would have cost us the dependency edge
    assert by_key["DATABASE_URL"].host == "db.internal"
    assert by_key["DATABASE_URL"].port == "5432"
    assert by_key["ORDERS_SERVICE_HOST"].value == "orders-svc"


def test_opaque_values_are_redacted_whatever_the_key_is_called():
    assert redact("wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY123") == "[redacted]"
    assert redact("orders-svc") == "orders-svc"
    assert redact("http://orders-svc:8080/v1") == "http://orders-svc:8080/v1"


def test_key_material_is_never_read(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("x = 1\n")
    for name in ("server.pem", "id_rsa", "prod.key", "my_password.py", "client_secret.py"):
        (tmp_path / name).write_text("PRIVATE\n")

    seen = {p.name for p in iter_files(tmp_path, (".py",))}
    assert seen == {"main.py"}          # the .py files named like secrets are excluded too


def test_a_repo_can_exclude_more_but_not_less(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("x = 1\n")
    (tmp_path / "generated").mkdir()
    (tmp_path / "generated" / "pb2.py").write_text("x = 1\n")
    (tmp_path / "server.pem").write_text("PRIVATE\n")

    # a repo that tries to opt back into reading key material
    (tmp_path / ".contextignore").write_text("# ours\ngenerated/\n")
    patterns = load_contextignore(tmp_path)

    assert "generated" in patterns                       # the repo's own addition applies
    for always in ALWAYS_EXCLUDE:
        assert always in patterns                        # and cannot be dropped

    assert {p.name for p in iter_files(tmp_path, (".py",))} == {"main.py"}


def test_dotenv_is_read_by_default_and_can_be_opted_out(tmp_path):
    """Reading .env is the point: it is where service addresses live. But a repo that would
    rather it were not read has to be able to say so."""
    (tmp_path / ".env").write_text("ORDERS_SERVICE_HOST=orders-svc\n")

    assert [p.name for p in _config_files(tmp_path, 3)] == [".env"]
    assert [b.key for b in config_hosts(tmp_path)] == ["ORDERS_SERVICE_HOST"]

    (tmp_path / ".contextignore").write_text(".env\n")
    assert _config_files(tmp_path, 3) == []
    assert config_hosts(tmp_path) == []
