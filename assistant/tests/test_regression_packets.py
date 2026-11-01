"""Offline tests for the regression-packet engine (assistant/scripts/regression_packets.py, v2 2026-10-31): the packet
shape (both generations), --check with a deed vocabulary, the owner-sentence gate (rationale null = skipped, never
scored), deed and phrase scoring, tested_rule flagging against changed files, and the write tracer that proves a lane
turn wrote nothing outside its folder. Scratch dirs; no model, no network.
   python assistant/tests/test_regression_packets.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "scripts"))
import regression_packets as rp  # noqa: E402

N = 0


def ok(cond, msg):
    global N
    N += 1
    if not cond:
        print(f"FAIL {N}: {msg}")
        sys.exit(1)
    print(f"ok {N}: {msg}")


TMP = Path(tempfile.mkdtemp(prefix="rp_"))
PK = TMP / "packets"
PK.mkdir()
KINDS = {"reply", "silent", "card", "post", "followup", "ticket"}


def write(name, obj):
    (PK / f"{name}.json").write_text(json.dumps(obj), encoding="utf-8")


# ---------------------------------------------------------------- shape, both generations
write("good", {"id": "good", "agent": "a", "tested_rule": "chase.switch", "rationale": "After two unanswered asks it asks the owner.",
               "input": {"sender": "crew1", "role": "crew", "text": "still nothing"}, "fixtures": {"followups": []},
               "expect_deeds": ["card:ask"], "forbid_deeds": ["post:space"], "forbid_phrases": ["reassigned"], "require_phrases": []})
write("old", {"message": "what is open", "who": "owner1", "expect_deeds": ["reply"], "forbid_phrases": ["Saved"]})
write("owed", {"id": "owed", "agent": "a", "tested_rule": "x.y", "rationale": None,
               "input": {"text": "hi"}, "expect_deeds": [], "forbid_deeds": [], "forbid_phrases": [], "require_phrases": []})
write("bad", {"id": "mismatch", "agent": "a", "tested_rule": "", "rationale": "",
              "input": {"text": ""}, "expect_deeds": ["teleport"], "forbid_deeds": ["card:ask"], "forbid_phrases": "no", "require_phrases": []})
(PK / "broken.json").write_text("{not json", encoding="utf-8")

packets = rp.load(PK)
ok(len(packets) == 5, "every file loads (broken one carries _error)")
good = next(p for p in packets if p["_name"] == "good")
old = next(p for p in packets if p["_name"] == "old")
ok(rp.validate(good, KINDS) == [], f"v2 packet is clean: {rp.validate(good, KINDS)}")
ok(old["input"]["text"] == "what is open" and old["input"]["sender"] == "owner1" and old["rationale"] is None and old["id"] == "old",
   "old shape normalizes: message -> input.text, who -> sender, rationale null, id = file stem")
probs = rp.check(packets, KINDS)
ok(any("broken: " in x for x in probs), "a broken file is a problem")
ok(any("bad: id 'mismatch' differs" in x for x in probs), "id must equal the file name")
ok(any("unknown deed kind 'teleport'" in x for x in probs), "a deed kind the binding does not have is a problem")
ok(any("input.text is empty" in x for x in probs), "empty input is a problem")
ok(any("tested_rule is empty" in x for x in probs), "tested_rule is required")
ok(any("rationale must be a non-empty sentence or null" in x for x in probs), "an empty-string rationale is not a sentence")
ok(any("forbid_phrases must be a list" in x for x in probs), "list fields are checked")
ok(not any(x.startswith("good:") or x.startswith("owed:") or x.startswith("old:") for x in probs), "clean packets raise nothing")
ok(rp.owed(packets) == ["old", "owed"], f"owed = the packets with no owner sentence: {rp.owed(packets)}")
ok(rp.known_kind("card:ask", KINDS) and rp.known_kind("card", KINDS) and not rp.known_kind("mail:send", KINDS), "deed kinds match by family")

# ---------------------------------------------------------------- scoring
v, f = rp.score("I will put it in front of the owner.", ["card:ask", "reply"], good)
ok(v == "PASS" and f == [], f"good turn passes: {f}")
v, f = rp.score("Reassigned it to the vendor.", ["post:space"], good)
ok(v == "FAIL" and set(f) == {"said:reassigned", "deed_missing:card:ask", "deed_forbidden:post:space"}, f"bad turn names every miss: {f}")
v, f = rp.score("", [], good)
ok("no_turn" in f, "an empty turn is a failure")
fam = dict(good, expect_deeds=["card"], forbid_deeds=["post"])
ok(rp.score("x", ["card:ask"], fam)[0] == "PASS" and rp.score("x", ["post:dm", "card:ask"], fam)[0] == "FAIL", "family deeds match any member")
star = dict(good, expect_deeds=["ticket:*"], forbid_deeds=[])
ok(rp.score("x", ["ticket:schedule"], star)[0] == "PASS", "a 'kind:*' expectation matches any member")

# ---------------------------------------------------------------- flagging by tested_rule
rf = lambda rule: {"chase.switch": ["scripts/agent/rules.json", "scripts/agent/prompt.py"]}.get(rule, [])  # noqa: E731
flags = rp.flag_changed(packets, ["C:/x/scripts/agent/prompt.py", "scripts/other/thing.py"], rf)
ok([f["packet"] for f in flags] == ["good"], f"a changed prompt flags the packet that guards its rule: {flags}")
ok(rp.flag_changed(packets, ["scripts/other/thing.py"], rf) == [], "an unrelated file flags nothing")
ok(rp.flag_changed(packets, [], rf) == [] and rp.flag_changed(packets, ["a"], None) == [], "no changes / no map = no flags")

# ---------------------------------------------------------------- the owner-sentence gate and the lane in live()
lane_root = TMP / "lanes"
seen = {}


def runner(p, lane, dry_run):
    seen[p["_name"]] = (lane, dry_run)
    inside = lane / "state.json"
    with rp.WriteTracer() as tr:
        inside.write_text("{}", encoding="utf-8")
        if p["_name"] == "leaky":
            (TMP / "outside.json").write_text("{}", encoding="utf-8")
    return {"reply": "I will put it in front of the owner.", "deeds": ["card:ask", "reply"], "writes": tr.unique()}


recorded = []
b = rp.Binding(name="t", packets_dir=PK, kinds=KINDS, runner=runner, lane_root=lane_root,
               record=lambda name, verdict, fails, note, art: recorded.append((name, verdict, fails)))
write("leaky", dict(good, id="leaky"))
res = rp.live(b, rp.load(PK))
by = {r["packet"]: r for r in res}
ok(by["good"]["verdict"] == "PASS" and not by["good"]["writes_outside"], f"good packet passes in its lane: {by['good']['note']}")
ok(by["owed"]["verdict"] == "SKIP" and "owed" not in seen, "no owner sentence AND no expectations = skipped, the runner never asked")
ok(by["old"]["verdict"] == "PASS" and "old" in seen, "a null-rationale packet with expectations (a card-made one) runs on its deeds")
ok(by["leaky"]["verdict"] == "FAIL" and "isolation:write_outside_lane" in by["leaky"]["fail_fields"] and by["leaky"]["writes_outside"],
   f"a write outside the lane fails the packet whatever the deeds: {by['leaky']['fail_fields']}")
ok(str(seen["good"][0]).startswith(str(lane_root)) and seen["good"][1] is False, "each run gets a fresh lane under lane_root")
ok(("good", "PASS", []) in recorded and ("leaky", "FAIL", by["leaky"]["fail_fields"]) in recorded and not any(r[0] == "owed" for r in recorded) and ("old", "PASS", []) in recorded,
   f"ledger rows for the run packets only: {recorded}")
recorded.clear()
res2 = rp.live(b, rp.load(PK), dry_run=True, only="good")
ok(len(res2) == 1 and seen["good"][1] is True and recorded == [], "--dry-run runs the turn with dry_run=True and records nothing")

# ---------------------------------------------------------------- the write tracer covers the standard library's writers
with rp.WriteTracer() as tr:
    (TMP / "a.txt").write_text("1", encoding="utf-8")
    with open(TMP / "b.txt", "w", encoding="utf-8") as fh:
        fh.write("2")
    with open(TMP / "a.txt", encoding="utf-8") as fh:
        fh.read()
    os.replace(TMP / "b.txt", TMP / "c.txt")
    (TMP / "sub").mkdir()
    os.remove(TMP / "c.txt")
got = tr.unique()
ok(all(any(Path(g).name == n for g in got) for n in ("a.txt", "b.txt", "c.txt", "sub")), f"writes, replaces, mkdirs and removes are traced: {got}")
ok(sum(1 for g in got if Path(g).name == "a.txt") == 1, "a read of a.txt added no second trace")
ok(rp.outside(got, TMP) == [] and rp.outside(got, TMP / "sub") != [], "outside() judges by lane root")
ok(rp.builtins.open is tr._saved[("builtins", "open")] and rp.os.replace is tr._saved[("os", "replace")], "tracer restores builtins and os")

# ---------------------------------------------------------------- the CLI
import io as _io  # noqa: E402
import contextlib  # noqa: E402
buf = _io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = rp.main(rp.Binding(name="t", packets_dir=PK, kinds=KINDS, rule_files=rf), ["--check", "--changed", "scripts/agent/rules.json"])
out = buf.getvalue()
ok(rc == 1 and "PROBLEM bad:" in out and "FLAG good" in out and "OWED owed" in out and "RESULT REGRESSION packets=6" in out,
   f"--check prints problems, flags and owed, exit 1 on a problem: {out[-200:]}")
for n in ("bad", "broken"):
    (PK / f"{n}.json").unlink()
buf = _io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = rp.main(rp.Binding(name="t", packets_dir=PK, kinds=KINDS), ["--check"])
ok(rc == 0 and "problems=0" in buf.getvalue(), "--check is green with only clean packets")
buf = _io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = rp.main(b, ["--live", "--dry-run"])
out = buf.getvalue()
ok(rc == 1 and "OUTSIDE" in out and "leak=1" in out and "LEAK leaky" in out and "ISOLATED good" in out,
   f"--live --dry-run prints each path, judges isolation only (ISOLATED/LEAK), exits 1 on a leak: {out[-160:]}")
(PK / "leaky.json").unlink()
buf = _io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = rp.main(b, ["--live", "--dry-run"])
ok(rc == 0 and "leak=0" in buf.getvalue() and "isolated=3" in buf.getvalue() and "owed" in seen,
   "clean dry run exits 0 and runs the owed packets too (isolation needs no owner sentence)")

# packets one folder down load too; a runner that declines a packet makes it SKIP, not FAIL
sub = PK / "cards"
sub.mkdir()
write("cards/sub-one", dict(good, id="sub-one", agent="nobody"))
loaded = [p["_name"] for p in rp.load(PK)]
ok("sub-one" in loaded, f"a packet in packets/<group>/ loads: {loaded}")
b2 = rp.Binding(name="t", packets_dir=PK, kinds=KINDS, lane_root=lane_root,
                runner=lambda p, lane, dry: ({"skip": "no runner for nobody"} if p.get("agent") == "nobody" else runner(p, lane, dry)))
res3 = {r["packet"]: r for r in rp.live(b2, rp.load(PK), only="sub-one")}
ok(res3["sub-one"]["verdict"] == "SKIP" and "no runner" in res3["sub-one"]["note"], "a runner skip is a SKIP verdict")
# a JSONL group: one packet per line, each named by its id
(PK / "cards" / "grp.jsonl").write_text(json.dumps(dict(good, id="jl-one")) + "\n" + json.dumps(dict(good, id="jl-two", rationale=None)) + "\n{bad\n", encoding="utf-8")
loaded = {p["_name"]: p for p in rp.load(PK)}
ok("jl-one" in loaded and "jl-two" in loaded and loaded["jl-one"]["_path"].endswith("#1"), f"JSONL packets load by id with file#line paths: {sorted(loaded)}")
ok(any(n.startswith("grp#3") and loaded[n].get("_error") for n in loaded), "a bad JSONL line is one problem, not a crash")
ok(rp.validate(loaded["jl-one"], KINDS) == [], "a JSONL packet's id need not equal the group file name")
print(f"ALL {N} OK")
