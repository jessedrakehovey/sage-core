"""Offline tests for assistant/agent/provenance.py (Seed B, truth with provenance): as_of parsing including the relative
forms a replay fixture uses, staleness per kind from the data table (null = durable), stale reads as UNKNOWN with the
last known value kept, sentinel rules from the data table (filtered count zero, null schedule timestamp, windowed
absence, cached null count) read as UNKNOWN, merge keeps a conflict side by side instead of letting the first source
win, gate/describe lines, and the engine files carry no organization. No network, no model, scratch files only.
   python assistant/tests/test_provenance.py
"""
import json
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "agent"))
import provenance as pv  # noqa: E402

N = 0


def ok(cond, msg):
    global N
    N += 1
    if not cond:
        print(f"FAIL {N}: {msg}")
        sys.exit(1)
    print(f"ok {N}: {msg}")


NOW = datetime(2026, 10, 31, 12, 0, 0)
RULES = pv.load_rules()
SENT = pv.load_sentinels()

# --- time
ok(pv.parse_when("2026-10-31T09:12", NOW) == datetime(2026, 10, 31, 9, 12), "ISO minute stamp parses")
ok(pv.parse_when("10/29/2026 at 08:00 AM", NOW) == datetime(2026, 10, 29, 8, 0), "MM/DD/YYYY at HH:MM AM parses")
ok(pv.parse_when("now-50h", NOW) == datetime(2026, 10, 29, 10, 0), "relative now-50h parses against the given now")
ok(pv.parse_when("now - 3d", NOW) == datetime(2026, 10, 28, 12, 0), "relative now - 3d with spaces parses")
ok(pv.parse_when("now", NOW) == NOW and pv.parse_when("", NOW) is None and pv.parse_when("garbage", NOW) is None, "now / empty / garbage")
ok(pv.age_hours("2026-10-31T09:00:00", NOW) == 3.0, "age in hours")
ok(pv.short("2026-10-31T09:12", NOW) == "09:12" and pv.short("2026-10-29T08:00", NOW) == "10/29 08:00", "short forms")

# --- rules are data
ok(pv.max_age_hours("work_board", RULES) == 30 and pv.max_age_hours("memory", RULES) is None, "table: board 30 h, memory durable")
ok(pv.max_age_hours("never_listed_kind", RULES) is None, "an unlisted kind takes the default (durable)")
custom = {"max_age_hours": {"x": 1}, "default_max_age_hours": 2}
ok(pv.max_age_hours("y", custom) == 2, "a custom table's default applies to unlisted kinds")

# --- stale reads as UNKNOWN, value kept
f = pv.fact("open_jobs", 3, "now-50h", "work_board.json", "work_board", now=NOW, rules=RULES, sentinels=SENT)
ok(f["stale"] and f["unknown"] and pv.known(f) is None and f["value"] == 3, "50 h old board fact: stale, unknown, last value kept")
ok("50 h old" in f["reason"] and "over 30 h" in f["reason"], f"reason names age and limit: {f['reason']}")
ok("UNKNOWN" in pv.describe(f, NOW) and "last known 3" in pv.describe(f, NOW), f"describe: {pv.describe(f, NOW)}")
g = pv.fact("open_jobs", 3, "now-2h", "work_board.json", "work_board", now=NOW, rules=RULES, sentinels=SENT)
ok(not g["stale"] and not g["unknown"] and pv.known(g) == 3, "2 h old board fact is known")
ok(pv.describe(g, NOW) == "open_jobs 3 (work_board.json 10:00)", f"known describe: {pv.describe(g, NOW)}")
h = pv.fact("open_jobs", 3, "", "work_board.json", "work_board", now=NOW, rules=RULES, sentinels=SENT)
ok(h["unknown"] and "no as_of" in h["reason"], "a timed kind with no as_of is unknown")
d = pv.fact("household", ["Pat"], "", "note.md", "wiki_note", now=NOW, rules=RULES, sentinels=SENT)
ok(not d["unknown"] and pv.known(d) == ["Pat"], "a durable kind with no as_of is known")
e = pv.fact("household", ["Pat"], "2025-01-01", "note.md", "wiki_note", now=NOW, rules=RULES, sentinels=SENT)
ok(not e["stale"], "a durable kind never goes stale however old")

# --- sentinels are data
s1 = pv.fact("count", 0, "now", "report work_order (unit filter)", "live_report", field="count", context={"filtered": True},
             now=NOW, rules=RULES, sentinels=SENT)
ok(s1["unknown"] and s1["sentinel"] == "filtered_report_count_zero", f"filtered count 0 trips the sentinel: {s1['reason']}")
s1b = pv.fact("count", 0, "now", "report work_order", "live_report", field="count", context={"filtered": False},
              now=NOW, rules=RULES, sentinels=SENT)
ok(not s1b["unknown"] and pv.known(s1b) == 0, "an UNfiltered count 0 is a real zero")
s2 = pv.fact("scheduled", None, "now", "work_board.json", "work_board", field="scheduled", context={"status_in": "Scheduled"},
             now=NOW, rules=RULES, sentinels=SENT)
ok(s2["unknown"] and s2["sentinel"] == "report_null_schedule_timestamp", "null schedule on a Scheduled row is unknown")
s2b = pv.fact("scheduled", None, "now", "work_board.json", "work_board", field="scheduled", context={"status_in": "New"},
              now=NOW, rules=RULES, sentinels=SENT)
ok(not s2b["unknown"], "null schedule on a New row is just empty (no sentinel)")
s3 = pv.fact("exists", False, "now", "report work_order 04/01-07/25", "live_report", field="exists", context={"windowed": True},
             now=NOW, rules=RULES, sentinels=SENT)
ok(s3["unknown"] and s3["sentinel"] == "windowed_report_absent", "absence from a windowed pull is unknown")
s4 = pv.fact("photo_count", None, "now", "work_board.json", "work_board", field="photo_count", now=NOW, rules=RULES, sentinels=SENT)
ok(s4["unknown"] and s4["sentinel"] == "cached_count_null", "a null cached count is unknown")
s4b = pv.fact("photo_count", 0, "now", "work_board.json", "work_board", field="photo_count", now=NOW, rules=RULES, sentinels=SENT)
ok(not s4b["unknown"], "a cached count of 0 is a counted zero")
# a stale AND sentinel reading names both
s5 = pv.fact("photo_count", None, "now-3d", "work_board.json", "work_board", field="photo_count", now=NOW, rules=RULES, sentinels=SENT)
ok(s5["stale"] and s5["sentinel"] and "sentinel cached_count_null" in s5["reason"] and "over" in s5["reason"], "stale + sentinel reason names both")
ok(pv.sentinel_hits(0, "live_report", "x", "count", {"filtered": True}, [{"name": "empty", "when": {}}]) == [], "a rule with no conditions never trips")

# --- merge: agreement, conflict kept side by side, unknown never votes
a = pv.fact("status", "Scheduled", "now-1h", "work_board.json", "work_board", now=NOW, rules=RULES, sentinels=SENT)
b = pv.fact("status", "scheduled", "now-2h", "turn_state.json", "work_board", now=NOW, rules=RULES, sentinels=SENT)
m = pv.merge([a, b], NOW)
ok(not m["conflict"] and pv.known(m) == "Scheduled" and m["sources"] == ["work_board.json", "turn_state.json"], "agreeing sources merge to the freshest with both listed")
c = pv.fact("status", "Completed", "now-2h", "turn_state.json", "work_board", now=NOW, rules=RULES, sentinels=SENT)
m2 = pv.merge([a, c], NOW)
ok(m2["conflict"] and m2["unknown"] and pv.known(m2) is None, "disagreeing sources: conflict, unknown, no winner")
ok(len(m2["candidates"]) == 2 and {x["value"] for x in m2["candidates"]} == {"Scheduled", "Completed"}, "both candidates kept side by side")
ok("work_board.json says 'Scheduled'" in m2["reason"] and "turn_state.json says 'Completed'" in m2["reason"], f"reason names both sides: {m2['reason']}")
stale_c = pv.fact("status", "Completed", "now-3d", "turn_state.json", "work_board", now=NOW, rules=RULES, sentinels=SENT)
m3 = pv.merge([a, stale_c], NOW)
ok(not m3["conflict"] and pv.known(m3) == "Scheduled" and len(m3["candidates"]) == 2, "a stale reading never votes but stays visible as a candidate")
m4 = pv.merge([stale_c], NOW)
ok(m4["unknown"] and m4["value"] is None and m4["candidates"][0]["value"] == "Completed", "only stale readings: unknown with the candidates")
ok(pv.merge([], NOW)["unknown"], "no readings: unknown")

# --- gate / verdict lines
gt = pv.gate([g, f, m2, s1])
ok(gt["known"] == {"open_jobs": 3} and set(gt["unknown"]) == {"open_jobs", "status", "count"} and gt["conflicts"] == ["status"], f"gate: {gt}")
ok(len(pv.verdict_lines([g, f, m2], NOW)) == 2 and all("UNKNOWN" in ln for ln in pv.verdict_lines([g, f, m2], NOW)), "verdict lines list only what may not be asserted")

# --- configure() with an organization's own tables
tmp = Path(tempfile.mkdtemp(prefix="pv_"))
(tmp / "rules.json").write_text(json.dumps({"max_age_hours": {"thing": 1}}), encoding="utf-8")
(tmp / "sent.json").write_text(json.dumps({"sentinels": [{"name": "zero_thing", "when": {"kind": "thing", "value_in": [0]}, "why": "w"}]}), encoding="utf-8")
pv.configure(rules=tmp / "rules.json", sentinels=tmp / "sent.json")
t1 = pv.fact("n", 0, "now", "src", "thing", now=NOW)
ok(t1["sentinel"] == "zero_thing" and pv.max_age_hours("thing") == 1, "configure() sets the module defaults from an organization's files")
pv.configure(rules=pv.RULES_FILE, sentinels=pv.SENTINELS_FILE)

# --- CLI
(tmp / "facts.json").write_text(json.dumps([{"fact": "a", "value": 1, "as_of": "now", "source": "s", "kind": "memory"}]), encoding="utf-8")
import io, contextlib  # noqa: E401
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = pv.main(["describe", str(tmp / "facts.json")])
ok(rc == 0 and "a 1 (s" in buf.getvalue(), f"describe CLI: {buf.getvalue().strip()}")

# --- engine files carry no organization: the check reads the identity words from the engine's own classifier
# (engine.org_values: org.json / people.json / core_sync.json of whatever pack is present), never from a literal list
# here -- a literal list of an organization's names would itself put that organization into engine code (found by
# the personal-side sync on 2026-11-01). A production pack repeats this check on its vendored copy with its own names.
sys.path.insert(0, str(ROOT))
try:
    import engine as _eng
    ov = _eng.org_values()
    words = {str(w) for k in ("names", "words", "hosts", "domains") for w in (ov.get(k) or []) if len(str(w)) > 3}
    words.add(str(ov.get("org_name") or "")) if len(str(ov.get("org_name") or "")) > 3 else None
except Exception:  # noqa: BLE001
    words = set()
for p in (ROOT / "agent" / "provenance.py", ROOT / "agent" / "provenance_rules.json", ROOT / "agent" / "sentinels.json"):
    txt = p.read_text(encoding="utf-8").lower()
    hits = sorted(w for w in words if __import__("re").search(r"(?<![a-z])" + __import__("re").escape(str(w).lower()) + r"(?![a-z])", txt))   # whole words: 'Soren' is not 'variance'
    ok(not hits, f"{p.name} names no organization identity word: {hits[:3]}")
print(f"ALL {N} OK")
