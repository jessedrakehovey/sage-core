"""crm.py - the one door to the organization's CRM data (sage-core, 2026-10-29).

The engine ships with NO CRM. An organization's CRM is supported by a connector Sage builds the day it is
introduced to one (onboarding files the build; see assistant/CRM_CONTRACT.md for what a connector provides).
Every engine script that needs clients, projects, stages, meetings, tasks or forms reads them through here, so
the same code runs against any CRM, or none:

  crm.name()            what to call the CRM in a sentence ("the CRM" when none is connected)
  crm.connected()       a connector is installed (a plugin declares CRM = True) or a snapshot exists
  crm.snapshot_path()   where the connector writes its snapshot (env CRM_SNAPSHOT_FILE, config.json -> crm.snapshot,
                        the connector's SNAPSHOT_PATH, else assistant/data/crm.json)
  crm.snapshot()        the snapshot as a dict ({} when there is none): ts, stages, projects, contacts, meetings,
                        tasks, forms, money (CRM_CONTRACT.md)
  crm.field(rec, key)   a record's value under the contract's name, falling back to the connector's own older
                        names (its FIELD_ALIASES), so an organization's existing records never need rewriting
  crm.project_id(p) / crm.status(p) / crm.stage(p) / crm.thread_id(rec) / crm.forms(snap)

Nothing here names a CRM, a person or an organization.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent          # assistant/scripts
ROOT = HERE.parent                              # assistant/
VAULT = ROOT.parent
DEFAULT_SNAPSHOT = "assistant/data/crm.json"
GENERIC_NAME = "the CRM"
# the contract's field names (CRM_CONTRACT.md); a connector maps its own older names onto these
CONTRACT_FIELDS = ("crm_id", "crm_status", "crm_stage", "thread_id")


def _plugins() -> list:
    try:
        import alib
        return alib.plugins()
    except Exception:  # noqa: BLE001   no config, no plugins: the engine runs CRM-less
        return []


def _cfg() -> dict:
    try:
        import alib
        c = alib.config().get("crm") or {}
        return c if isinstance(c, dict) else {}
    except (Exception, SystemExit):  # noqa: BLE001
        return {}


def _connector():
    """The plugin that is the organization's CRM connector (CRM = True), or None."""
    for m in _plugins():
        if getattr(m, "CRM", False):
            return m
    return None


def name(default: str = GENERIC_NAME) -> str:
    n = str(_cfg().get("name") or "").strip()
    if n:
        return n
    c = _connector()
    return str(getattr(c, "SERVICE", "") or default) if c else default


def connected() -> bool:
    return _connector() is not None or snapshot_path().exists()


def snapshot_path() -> Path:
    env = os.environ.get("CRM_SNAPSHOT_FILE")
    if env:
        return Path(env)
    rel = str(_cfg().get("snapshot") or "")
    if not rel:
        c = _connector()
        rel = str(getattr(c, "SNAPSHOT_PATH", "") or "") if c else ""
    p = Path(rel or DEFAULT_SNAPSHOT)
    return p if p.is_absolute() else VAULT / p


def snapshot(path: Path | None = None) -> dict:
    p = Path(path) if path else snapshot_path()
    try:
        d = json.loads(p.read_text(encoding="utf-8-sig"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _aliases() -> dict:
    out: dict = {}
    for m in _plugins():
        a = getattr(m, "FIELD_ALIASES", None)
        if isinstance(a, dict):
            for k, v in a.items():
                out.setdefault(k, [])
                out[k] += [v] if isinstance(v, str) else list(v)
    return out


_ALIASES = None


def field(rec, key: str, default=""):
    """rec[key], else rec[<the connector's older name for key>], else default."""
    global _ALIASES
    if not isinstance(rec, dict):
        return default
    v = rec.get(key)
    if v not in (None, ""):
        return v
    if _ALIASES is None:
        _ALIASES = _aliases()
    for alt in _ALIASES.get(key, []):
        v = rec.get(alt)
        if v not in (None, ""):
            return v
    return default


def project_id(p) -> str:
    return str(field(p, "crm_id") or "")


def status(p) -> str:
    return str(field(p, "crm_status") or "")


def stage(p) -> str:
    return str(field(p, "crm_stage") or "")


def thread_id(rec) -> str:
    return str(field(rec, "thread_id") or "")


# ---------------------------------------------------------------- the write side: what the connector turns into its own actions
GENERIC_PREVIEW = "[CRM MESSAGE PREVIEW]"
CHANNEL = "service"          # the channel word for "a message in the client's CRM thread" (intents, scheduling, mining)


def _hook(attr: str):
    c = _connector()
    return getattr(c, attr, None) if c else None


def kinds(attr: str) -> set:
    """A connector's exec kinds by role: MESSAGE_KINDS, INBOUND_KINDS, STAGE_KINDS, RETRY_KINDS, FOREVER_GATED ..."""
    try:
        import alib
        return alib.plugin_set(attr)
    except Exception:  # noqa: BLE001
        return set()


def routes() -> dict:
    try:
        import alib
        return alib.plugin_map("ROUTES")
    except Exception:  # noqa: BLE001
        return {}


def preview_tag() -> str:
    """The tag a card's question carries before the exact text of a CRM message."""
    return str(_hook("PREVIEW_TAG") or GENERIC_PREVIEW)


def valid_thread(s) -> bool:
    """Is s an id the connector can message (a project / conversation thread)? Any non-empty id without a connector rule."""
    f = _hook("valid_thread")
    return bool(f(str(s or ""))) if callable(f) else bool(str(s or "").strip())


def message_exec(thread_id: str, body: str, subject: str = "", in_reply_to: str = "") -> dict | None:
    """{kind, payload} for a message in a client's CRM thread, or None when no connector can send one."""
    f = _hook("message_exec")
    return f(str(thread_id), body, subject, in_reply_to) if callable(f) else None


def stage_exec(thread_id: str, stage: str) -> dict | None:
    """{kind, payload} that moves a project to a pipeline stage, or None when the connector cannot."""
    f = _hook("stage_exec")
    return f(str(thread_id), stage) if callable(f) else None


def apply_stage(thread_id: str, stage: str, dry: bool = False) -> tuple:
    """(ok, detail): move a project's stage now through the connector (an owner's tap already approved it)."""
    f = _hook("apply_stage")
    if not callable(f):
        return False, "no CRM connector can move a stage"
    return f(str(thread_id), stage, dry)


def booked_meeting(thread_id: str, who: str = ""):
    """The meeting a client already booked through the CRM's scheduler, or None."""
    f = _hook("booked_meeting")
    try:
        return f(str(thread_id or ""), who) if callable(f) else None
    except Exception:  # noqa: BLE001
        return None


def forms(snap: dict | None = None) -> list:
    s = snap if snap is not None else snapshot()
    v = s.get("forms")
    if v is None:
        for alt in _aliases().get("forms", []):
            v = s.get(alt)
            if v is not None:
                break
    return v if isinstance(v, list) else []


# ---------------------------------------------------------------- check: does the snapshot follow the contract
LISTS = ("stages", "projects", "contacts", "meetings", "tasks")


def check(snap: dict | None = None) -> list:
    """Problems with the snapshot against CRM_CONTRACT.md ([] when it is fine, or when there is no CRM at all)."""
    s = snapshot() if snap is None else snap
    if not s:
        return [] if not connected() else [f"no snapshot at {snapshot_path()}"]
    out = []
    for k in LISTS:
        if k in s and not isinstance(s[k], list):
            out.append(f"{k} must be a list")
    ids = {st.get("stage_id") for st in s.get("stages") or [] if isinstance(st, dict)}
    for p in s.get("projects") or []:
        if not isinstance(p, dict) or not p.get("id"):
            out.append("a project without an id")
        elif p.get("stage_id") and ids and p["stage_id"] not in ids:
            out.append(f"project {p['id']} points at an unknown stage")
    if not s.get("ts"):
        out.append("no ts (when the snapshot was taken)")
    return out


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="the organization's CRM through its connector")
    ap.add_argument("cmd", choices=["status", "check", "operator-notes"])
    a = ap.parse_args()
    if a.cmd == "operator-notes":
        notes = str(_hook("OPERATOR_NOTES") or "").strip()
        if notes:
            print(f"## CRM CONNECTOR ({name()})")
            print()
            print("The organization's CRM connector: these are its exact kinds, payloads and tags.")
            print()
            print(notes)
        return 0
    if a.cmd == "status":
        s = snapshot()
        print(f"CRM {name()}: {'connected' if connected() else 'none'}; snapshot {snapshot_path()} "
              f"({len(s.get('projects') or [])} projects, {len(s.get('contacts') or [])} contacts, ts {s.get('ts') or '-'})")
        return 0
    probs = check()
    print("CRM_CHECK ok" if not probs else "CRM_CHECK " + "; ".join(probs[:8]))
    return 1 if probs else 0


if __name__ == "__main__":
    import sys as _sys
    _sys.path.insert(0, str(HERE))
    raise SystemExit(main())
