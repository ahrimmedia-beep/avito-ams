"""Simple JSON-backed shared state for agents."""

from __future__ import annotations

import copy
import fcntl
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from avito_ams.tenant_id import validate_tenant_id

# "lidogen" is the lead generation agent (budget and bid decisions).
_DEFAULT_STATE = {"items": {}, "chats": {}, "last_lidogen_run": None}


@contextmanager
def _file_lock(path: Path):
    """Acquire exclusive file lock (blocks until available)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("w") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    @classmethod
    def for_tenant(cls, tenant_id: str, data_root: Path) -> "StateStore":
        """Build a per-tenant StateStore at data_root/tenants/{tenant_id}/state.json."""
        validate_tenant_id(tenant_id)
        return cls(Path(data_root) / "tenants" / tenant_id / "state.json")

    def read(self) -> Dict[str, Any]:
        if not self.path.exists():
            return copy.deepcopy(_DEFAULT_STATE)
        with self.path.open(encoding="utf-8") as f:
            data = json.load(f)
        for k, v in _DEFAULT_STATE.items():
            data.setdefault(k, v)
        return data

    def write(self, state: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)

    def update_item(self, item_id: str, fields: Dict[str, Any]) -> None:
        with _file_lock(self.path):
            state = self.read()
            state["items"].setdefault(item_id, {}).update(fields)
            self.write(state)

    def update_chat(self, chat_id: str, fields: Dict[str, Any]) -> None:
        with _file_lock(self.path):
            state = self.read()
            state["chats"].setdefault(chat_id, {}).update(fields)
            self.write(state)

    def mark_lidogen_run(self) -> None:
        with _file_lock(self.path):
            state = self.read()
            state["last_lidogen_run"] = datetime.now(timezone.utc).isoformat()
            self.write(state)
