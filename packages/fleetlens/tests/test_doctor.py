

def test_scip_ruby_hint_names_a_platform(monkeypatch):
    """scip-ruby publishes no generic gem, so `gem install scip-ruby` fails everywhere with
    a message that suggests the same name back at you. The hint has to carry --platform."""
    import platform as pl

    from fleetlens import doctor

    monkeypatch.setattr(doctor.sys, "platform", "linux")
    monkeypatch.setattr(pl, "machine", lambda: "x86_64")
    assert doctor._scip_ruby_hint() == "gem install scip-ruby --platform x86_64-linux"

    monkeypatch.setattr(doctor.sys, "platform", "win32")
    assert "releases" in doctor._scip_ruby_hint()   # nothing published; point at the source
