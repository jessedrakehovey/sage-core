"""Offline tests for sense_editor.py's newest-word check (2026-10-21): an edit nudge names the step the owner is
really on, and a project whose newest word says "don't nudge" gets no ticket. Scratch ASSISTANT_DATA; no network."""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="wv_sed_"))
os.environ["ASSISTANT_DATA"] = str(TMP)
os.environ["WV_NO_TRIAGE"] = "1"
sys.path.insert(0, str(HERE.parent / "scripts"))
import sense_editor as se  # noqa: E402
import tickets  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def ago(days):
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")


se.MEMORY = TMP / "memory.json"
se.TRACKER = TMP / "project_state.json"
se.TRANSCRIPT = TMP / "transcript.jsonl"
SUBJ = "example Underhill (Underhill-Whitlock, 2026-06-20)"
OLD = {"id": "m62", "subject": SUBJ, "active": True, "last": ago(4),
       "fact": "the example edit is waiting on owner1's review of the best_of_GoPro sheets"}
HOLD = {"id": "m70", "subject": SUBJ, "active": True, "last": ago(3),
        "fact": "owner1 is finishing his selection of the spoken audio clips for the example edit. That is the step "
                "holding the edit up now, so don't nudge him on it while he works"}
OTHER = {"id": "m9", "subject": "example", "active": True, "last": ago(1), "fact": "example's dogs are nervous; don't nudge"}


def write_memory(*facts):
    se.MEMORY.write_text(json.dumps({"next_id": 99, "facts": list(facts)}), encoding="utf-8")


# ------------------------------------------------------------------ the newest word
write_memory(OLD, HOLD, OTHER)
se.TRANSCRIPT.write_text("", encoding="utf-8")
w = se.latest_word("example_example")
check("newest memory fact for the project wins (not the passed GoPro step, not another couple)",
      w and w["source"] == "memory" and "spoken audio" in w["text"], w)
check("'don't nudge him while he works' holds the project", se.on_hold(w, 14))
check("a hold older than hold_days lapses", not se.on_hold(dict(w, when=ago(20)), 14))
check("a plain status fact is no hold", not se.on_hold({"when": ago(1), "source": "memory", "text": OLD["fact"]}, 14))
check("'should not be nudged' reads as a hold", se.on_hold({"when": ago(1), "source": "chat", "text": "example: I should not be nudged on this"}, 14))

se.TRANSCRIPT.write_text("\n".join(json.dumps(r) for r in [
    {"ts": ago(1), "who": "owner1", "text": "example: audio picks done, now on the music"},
    {"ts": ago(0), "who": "Sage", "text": "example is waiting on the GoPro sheets"},
]) + "\n", encoding="utf-8")
w = se.latest_word("example_example")
check("an owner's newer chat line beats memory; Sage's own lines never count",
      w and w["source"] == "chat" and "music" in w["text"] and not se.on_hold(w, 14), w)
se.TRACKER.write_text(json.dumps({"projects": {"k": {"couple": "example", "stage": "editing", "updated": ago(0)}}}), encoding="utf-8")
w = se.latest_word("example_example")
check("the tracker counts when it is newest", w and w["source"] == "tracker" and "editing" in w["text"], w)
check("a project nobody mentioned has no word", se.latest_word("Nobody_Here") is None)
se.TRACKER.unlink()

# ------------------------------------------------------------------ the sensor end to end
root = TMP / "projects"
(root / "example_example").mkdir(parents=True)
(root / "example_example" / "wedding.json").write_text("{}", encoding="utf-8")
se._cfg = lambda: dict(se.DEFAULTS, projects_root=str(root), min_free_gb=0)
se.project_status = lambda wj: ([{"mark": "J", "stage": "review", "phase": "2 picture",
                                  "text": "J: follow editor/prompts/REVIEW.md on the sheets in: best_of_GoPro"}], "")

se.TRANSCRIPT.write_text("", encoding="utf-8")
write_memory(OLD)
se.sense_editor()
db = tickets._load()
gate = [t for t in db.values() if t["kind"] == "edit_gate"]
check("no hold: the gate ticket is made and carries the newest word",
      len(gate) == 1 and "Newest word (memory" in gate[0]["summary"] and "Post ONE card" in gate[0]["summary"]
      and gate[0]["payload"]["latest_word"]["text"] == OLD["fact"], gate and gate[0]["summary"])

write_memory(OLD, HOLD)
se.sense_editor()
db = tickets._load()
check("hold: the open gate ticket is dismissed, nothing new is made",
      [t["status"] for t in db.values() if t["kind"] == "edit_gate"] == ["dismissed"], [t["status"] for t in db.values()])

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
sys.exit(1 if FAILS else 0)
