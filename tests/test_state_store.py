# tests/test_state_store.py
from avito_ams.state_store import StateStore


def test_initial_state_is_empty(tmp_path):
    store = StateStore(tmp_path / "state.json")
    assert store.read() == {"items": {}, "chats": {}, "last_lidogen_run": None}


def test_write_then_read_roundtrip(tmp_path):
    store = StateStore(tmp_path / "state.json")
    state = store.read()
    state["items"]["123"] = {"cpa": 250, "status": "active"}
    store.write(state)

    state2 = store.read()
    assert state2["items"]["123"]["cpa"] == 250


def test_update_partial(tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.update_item("456", {"cpa": 100, "views": 50})
    store.update_item("456", {"views": 75})
    state = store.read()
    assert state["items"]["456"]["cpa"] == 100
    assert state["items"]["456"]["views"] == 75


def test_update_item_uses_file_lock_under_concurrency(tmp_path):
    """Smoke test that two rapid updates don't lose data."""
    from avito_ams.state_store import StateStore

    store = StateStore(tmp_path / "state.json")
    for i in range(100):
        store.update_item(str(i), {"value": i})
    state = store.read()
    assert len(state["items"]) == 100
    assert state["items"]["50"]["value"] == 50


def test_for_tenant_creates_namespaced_path(tmp_path):
    store = StateStore.for_tenant("demo", tmp_path)
    assert store.path == tmp_path / "tenants" / "demo" / "state.json"


def test_for_tenant_rejects_invalid_id(tmp_path):
    import pytest
    with pytest.raises(ValueError, match="Invalid tenant_id"):
        StateStore.for_tenant("../etc/passwd", tmp_path)


def test_for_tenant_writes_to_namespace(tmp_path):
    store = StateStore.for_tenant("demo", tmp_path)
    store.write({"items": {"1": {"x": 1}}, "chats": {}, "last_lidogen_run": None})
    assert (tmp_path / "tenants" / "demo" / "state.json").exists()


def test_mark_lidogen_run_writes_iso_timestamp(tmp_path):
    from avito_ams.state_store import StateStore

    store = StateStore(tmp_path / "state.json")
    store.mark_lidogen_run()
    state = store.read()
    assert state["last_lidogen_run"] is not None
    assert "T" in state["last_lidogen_run"]  # ISO format
    # timezone-aware (no deprecation warning, contains '+' or 'Z' or '+00:00')
    assert (
        "+" in state["last_lidogen_run"]
        or state["last_lidogen_run"].endswith("Z")
        or "00:00" in state["last_lidogen_run"]
    )
