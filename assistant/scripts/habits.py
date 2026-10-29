"""habits.py - the habit ledger: what the agent keeps doing becomes code (sage-core, 2026-10-27).

the owner: "every time Sage does something it should later ask itself if the skill would most likely be used again
... and if the answer is yes it should write code or scripts for it." A feeling is not a rule, so this counts:
every approved action (exec_tickets.jsonl, by kind and target shape) and every doer run (tickets.json agent_run,
by the first words of the job) is an ASK CLASS; a class with `min_verified` (config.json habits.min_verified,
default 3) VERIFIED successes (outcomes.jsonl) and no script yet is ready to codify. With habits.codify = true
the ready classes become ONE build request each through capability.file_build (the builder writes the script
and its test; the next occurrence runs the script with the model only checking the result). Codified paths
inherit the gate: a script that sends is still a send and still goes through exec_store, drain and the tap.

  python assistant/scripts/habits.py report          the ledger
  python assistant/scripts/habits.py tick            recount; file builds when codify is on. Last line: HABITS classes=<n> ready=<n> filed=<n>
  python assistant/scripts/habits.py mark <class> <script path>     record a class as codified by hand
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "agent"))
from alib import DATA, config, load_json, save_json  # noqa: E402

LEDGER = DATA / "habits.json"
WINDOW_DAYS = 60
STOP = {"the", "a", "an", "to", "for", "of", "in", "on", "and", "my", "our", "this", "that", "please", "can", "you"}


def _rows(path: Path) -> list:
    try:
        return [json.loads(ln) for ln in path.read_text(encoding="utf-8-sig").splitlines() if ln.strip()]
    except (OSError, ValueError):
        return []


def _fold(rows: list) -> dict:
    out = {}
    for r in rows:
        if r.get("id"):
            out.setdefault(r["id"], {}).update(r)
    return out


def _ts(s) -> float:
    try:
        return time.mktime(time.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return 0.0


def ask_class(kind: str, payload, what: str = "") -> str:
    """An exec kind is a class by itself (email_send, imessage_send, sheet_set...); a doer run is classed by the
    first content words of the job, so 'export the invoices from X' recurs as one class."""
    if kind and kind != "agent_run":
        return kind
    text = what or (payload.get("what") or payload.get("task") or payload.get("instruction") if isinstance(payload, dict) else str(payload or ""))
    words = [w for w in re.findall(r"[a-z0-9]+", str(text).lower()) if w not in STOP]
    return "do:" + "-".join(words[:4]) if words else "do:unknown"


def count() -> dict:
    cutoff = time.time() - WINDOW_DAYS * 86400
    tickets = _fold(_rows(DATA / "exec_tickets.jsonl"))
    outcomes = _fold(_rows(DATA / "outcomes.jsonl"))
    verified_refs = {str(o.get("ref") or o.get("ticket") or "") for o in outcomes.values() if o.get("state") == "verified"}
    failed_refs = {str(o.get("ref") or o.get("ticket") or "") for o in outcomes.values() if o.get("state") in ("failed", "unverified", "abandoned")}
    classes = {}
    for t in tickets.values():
        if _ts(t.get("ts")) < cutoff:
            continue
        payload = t.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {"what": payload}
        cls = ask_class(str(t.get("kind") or ""), payload or {})
        c = classes.setdefault(cls, {"done": 0, "verified": 0, "failed": 0, "last": "", "examples": []})
        if t.get("status") == "done":
            c["done"] += 1
        if t["id"] in verified_refs:
            c["verified"] += 1
        elif t["id"] in failed_refs or t.get("status") == "exception":
            c["failed"] += 1
        c["last"] = max(c["last"], str(t.get("ts") or ""))
        if len(c["examples"]) < 2 and isinstance(payload, dict):
            ex = str(payload.get("what") or payload.get("subject") or payload.get("to") or "")[:80]
            if ex:
                c["examples"].append(ex)
    store = load_json(DATA / "tickets.json", {}) or {}
    doer_tickets = list(store.values()) if isinstance(store, dict) and not store.get("tickets") else list((store.get("tickets") if isinstance(store, dict) else store) or [])
    for t in doer_tickets:
        if not isinstance(t, dict) or t.get("kind") != "agent_run" or _ts(t.get("created") or t.get("ts")) < cutoff:
            continue
        cls = ask_class("agent_run", t.get("payload") or {}, str(t.get("summary") or t.get("title") or t.get("subject") or ""))
        c = classes.setdefault(cls, {"done": 0, "verified": 0, "failed": 0, "last": "", "examples": []})
        if t.get("status") == "done":
            c["done"] += 1
            if t.get("id") in verified_refs or t.get("verified"):
                c["verified"] += 1
        c["last"] = max(c["last"], str(t.get("created") or t.get("ts") or ""))
    return classes


def tick(dry: bool = False) -> dict:
    cfg = (config().get("habits") or {}) if (DATA.parent / "config.json").exists() else {}
    min_verified = int(cfg.get("min_verified", 3))
    codify = bool(cfg.get("codify", False))
    led = load_json(LEDGER, {}) or {}
    led.setdefault("classes", {})
    led.setdefault("codified", {})
    led.setdefault("filed", {})
    classes = count()
    for cls, c in classes.items():
        led["classes"][cls] = c
    ready = [cls for cls, c in classes.items() if c["verified"] >= min_verified and cls not in led["codified"] and cls not in led["filed"]
             and not cls.startswith(("email_send", "imessage_send"))]     # a plain send has nothing to codify: the executor IS the code
    filed = 0
    if codify and not dry:
        try:
            import capability
            import core
            d = capability._load()
            owner = core.first_owner()
            for cls in ready:
                c = classes[cls]
                ask = (f"codify the recurring ask class '{cls}' ({c['verified']} verified runs in {WINDOW_DAYS} days; e.g. {'; '.join(c['examples'])}): "
                       f"a script or executor path that does it deterministically, with a verifier, so the model only checks the result")
                g = capability.open_gap(d, "capability", ask, "done by hand each time so far", owner, {}, "habits")
                key = capability.file_build(d, g, by="habits", kick=True)
                if key:
                    led["filed"][cls] = {"build": key, "when": time.strftime("%Y-%m-%dT%H:%M:%S")}
                    filed += 1
            capability._save(d)
        except Exception as e:  # noqa: BLE001
            led["last_error"] = f"{type(e).__name__}: {str(e)[:200]}"
    led["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    led["ready"] = ready
    if not dry:
        save_json(LEDGER, led)
    return {"classes": len(classes), "ready": ready, "filed": filed, "codify": codify, "min_verified": min_verified}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("report")
    p = sub.add_parser("tick"); p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("mark"); p.add_argument("cls"); p.add_argument("script")
    a = ap.parse_args()
    if a.cmd == "report":
        classes = count()
        led = load_json(LEDGER, {}) or {}
        for cls, c in sorted(classes.items(), key=lambda kv: -kv[1]["verified"]):
            tag = "codified" if cls in (led.get("codified") or {}) else ("filed" if cls in (led.get("filed") or {}) else "")
            print(f"{cls:34} done {c['done']:3}  verified {c['verified']:3}  failed {c['failed']:3}  last {c['last'][:10]}  {tag}")
        print(f"HABITS classes={len(classes)}")
    elif a.cmd == "tick":
        r = tick(dry=a.dry_run)
        print(f"HABITS classes={r['classes']} ready={len(r['ready'])} filed={r['filed']} codify={'on' if r['codify'] else 'off'}" + (" ready: " + ", ".join(r["ready"]) if r["ready"] else ""))
    elif a.cmd == "mark":
        led = load_json(LEDGER, {}) or {}
        led.setdefault("codified", {})[a.cls] = {"script": a.script, "when": time.strftime("%Y-%m-%dT%H:%M:%S")}
        save_json(LEDGER, led)
        print(f"marked {a.cls} as codified by {a.script}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
