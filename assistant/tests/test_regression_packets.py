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
ok(rp.owed(packets) == ["owed"], f"owed = the packets with neither an owner sentence nor an expectation: {rp.owed(packets)}")
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
carded = dict(good, expect_deeds=["card:ask"], forbid_deeds=[], require_phrases=["[PREVIEW]", "please send the keys"], forbid_phrases=["reassigned"])
v, f = rp.score("Nothing sent. Your edit is a new draft, carded again.", ["card:ask"], carded)
ok(v == "FAIL" and set(f) == {"missing:[PREVIEW]", "missing:please send the keys"}, f"with no said text a carded phrase is missing: {f}")
v, f = rp.score("Nothing sent. Your edit is a new draft, carded again.", ["card:ask"], carded,
                said=["Shown again for your read.\n[PREVIEW]\nTo: owner1\n\nPlease send the keys today."])
ok(v == "PASS" and f == [], f"phrases are read across reply + the carded text: {f}")
v, f = rp.score("Nothing sent.", ["card:ask"], carded, said=["[PREVIEW] please send the keys; I reassigned it"])
ok(v == "FAIL" and f == ["said:reassigned"], f"a forbidden phrase on the card counts too: {f}")
v, f = rp.score("Carded your edit. Your edit removed 'reassigned it to the vendor', so that line is gone.", ["card:ask"], carded,
                said=["[PREVIEW] please send the keys today."])
ok(v == "PASS" and f == [], f"a forbidden phrase is judged on the carded text, not on the reply's narration of what was dropped: {f}")
v, f = rp.score("I reassigned it.", ["post:dm"], dict(carded, require_phrases=[], expect_deeds=[]))
ok(v == "FAIL" and f == ["said:reassigned"], f"with nothing carded the reply is the text that is judged: {f}")

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
# suites: smoke runs only smoke packets; regression runs smoke + regression; the note records suite and scoring
write("sm", dict(good, id="sm", suite="smoke", scoring="deeds"))
write("rg", dict(good, id="rg", suite="regression", scoring="judge"))
write("fl", dict(good, id="fl", suite="full"))
names = lambda res: sorted(r["packet"] for r in res)  # noqa: E731
ok(names(rp.live(b, rp.load(PK), suite="smoke")) == ["sm"], "suite=smoke runs the smoke packets only")
ok(set(names(rp.live(b, rp.load(PK), suite="regression"))) >= {"sm", "rg"} and "fl" not in names(rp.live(b, rp.load(PK), suite="regression")), "suite=regression runs smoke + regression, not full")
ok("fl" in names(rp.live(b, rp.load(PK), suite="full")), "suite=full runs everything")
r_sm = next(r for r in rp.live(b, rp.load(PK), only="sm"))
ok("suite=smoke" in r_sm["note"] and "scoring=deeds" in r_sm["note"], f"the ledger note carries suite and scoring: {r_sm['note'][:80]}")
write("badsuite", dict(good, id="badsuite", suite="nightly", scoring="vibes"))
pr = rp.validate(next(p for p in rp.load(PK) if p["_name"] == "badsuite"), KINDS)
ok(any("suite must be" in x for x in pr) and any("scoring must be" in x for x in pr), f"unknown suite / scoring values are problems: {pr}")

# ---------------------------------------------------------------- the runner table (v2.2): process and server shapes
import http.server  # noqa: E402
import threading  # noqa: E402

RT = TMP / "rt"
RT.mkdir()
(RT / "pk").mkdir()
(RT / "pk" / "pa.json").write_text(json.dumps(dict(good, id="pa", agent="alpha")), encoding="utf-8")
(RT / "pk" / "grp.jsonl").write_text(json.dumps(dict(good, id="pb", agent="beta", expect_deeds=["card:ask"], forbid_deeds=["post:space"]))
                                     + "\n" + json.dumps(dict(good, id="pc", agent="gamma")) + "\n", encoding="utf-8")
# process shape: a script that writes one file in the lane and prints EVAL_RESULT; --dry-run appended on a dry run
script = RT / "turn.py"
script.write_text(
    "import json, sys\n"
    "args = sys.argv[1:]\n"
    "lane = args[args.index('--lane') + 1]; pk = json.load(open(args[args.index('--packet') + 1], encoding='utf-8'))\n"
    "open(lane + '/state.json', 'w').write('{}')\n"
    "print('noise'); print('EVAL_RESULT ' + json.dumps({'reply': 'I will put it in front of the owner.', 'deeds': ['card:ask', 'reply', 'dry' if '--dry-run' in args else 'live'],\n"
    "      'writes': [lane + '/state.json'], 'error': '', 'server_side': ['the transcript for ' + pk['id']]}))\n", encoding="utf-8")
spec_a = rp.RunnerSpec(mode="process", command=[sys.executable, str(script), "--lane", "{lane}", "--packet", "{packet}"])

# server shape: a fake chat endpoint that records the POST and answers after one poll
posted = []
asks = {}


class _H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        posted.append(body)
        if body.get("agent", "").endswith("-busy") and sum(1 for b in posted if b.get("agent") == body["agent"]) == 1:
            return self._send(409, {"error": "busy"})
        aid = "a%011d" % len(posted)
        asks[aid] = {"id": aid, "status": "running", "answer": "", "polls": 0}
        # the fake brain leaves a journal line in the lane, as a diverted card would
        Path(body["eval_lane"], "journal.jsonl").write_text(json.dumps({"kind": "card", "verb": "ask", "q": body["question"][:40]}) + "\n", encoding="utf-8")
        self._send(200, {"id": aid})

    def do_GET(self):
        aid = self.path.rsplit("=", 1)[-1]
        st = asks.get(aid)
        if not st:
            return self._send(404, {"error": "no such ask"})
        st["polls"] += 1
        if st["polls"] >= 2:
            st.update({"status": "done", "answer": "Card posted.", "steps": [{"tool": "Write", "path": str(RT / "elsewhere.txt")}]})
        self._send(200, st)


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"


def _deeds(lane, doc):
    out = []
    jf = Path(lane) / "journal.jsonl"
    if jf.exists():
        for ln in jf.read_text(encoding="utf-8").splitlines():
            r = json.loads(ln)
            out.append(f"{r['kind']}:{r['verb']}")
    return out


def _writes(lane, doc):
    return [str(st.get("path")) for st in (doc.get("steps") or []) if st.get("tool") == "Write" and st.get("path")]


def _seed(lane, packet):
    (Path(lane) / "journal.jsonl").write_text(json.dumps({"kind": "card", "verb": "ask", "q": "seeded"}) + "\n", encoding="utf-8")


spec_b = rp.RunnerSpec(mode="server", endpoint=base + "/ask", status=base + "/status?id={id}", brain="brain-b",
                       deeds=_deeds, writes=_writes, dry_seed=_seed, dry_answer="Noted (dry).", poll_s=0.05, timeout_s=10,
                       server_side=["the server's transcript for {agent} ({id})"],
                       question=lambda pk, lane: "Q: " + str(pk["input"]["text"]))
runner = rp.table_runner({"alpha": spec_a, "beta": spec_b})
lane_root2 = RT / "lanes"
b3 = rp.Binding(name="t", packets_dir=RT / "pk", kinds=KINDS, runner=runner, lane_root=lane_root2)
res = {r["packet"]: r for r in rp.live(b3, rp.load(RT / "pk"))}
ok(res["pa"]["verdict"] == "PASS" and "live" in res["pa"]["deeds"] and "server-side (not traced): the transcript for pa" in res["pa"]["note"],
   f"process shape: argv template filled, EVAL_RESULT parsed, server_side named: {res['pa']['note'][:120]}")
ok(res["pc"]["verdict"] == "SKIP" and "no runner for agent 'gamma'" in res["pc"]["note"], "an agent with no row in the table is skipped")
ok(res["pb"]["verdict"] == "FAIL" and "isolation:write_outside_lane" in res["pb"]["fail_fields"] and str(RT / "elsewhere.txt") in res["pb"]["writes_outside"],
   f"server shape: the binding's writes reader can fail a turn for a write outside the lane: {res['pb']['fail_fields']}")
ok("card:ask" in res["pb"]["deeds"] and res["pb"]["reply"] == "Card posted." and "brain-b-eval-pb" in res["pb"]["note"],
   f"server shape: deeds read from the lane journal, the reply from the status document, the eval agent named: {res['pb']['note'][:140]}")
pb_post = next(b for b in posted if b["agent"] == "brain-b-eval-pb")
ok(pb_post["question"].startswith("Q: ") and Path(pb_post["eval_lane"]).is_dir() and str(pb_post["eval_lane"]).startswith(str(lane_root2)),
   "server shape: the POST carries the composed question and the lane path under the lane root")
lane_files = {Path(w).name for w in res["pb"]["writes"]}
ok({"packet.json", "question.txt", "ask_status.json"} <= lane_files, f"server shape: the runner's own writes (packet, question, status copy) are traced in the lane: {sorted(lane_files)}")
# dry run: no request, the seed stands in for the brain
n_before = len(posted)
dres = {r["packet"]: r for r in rp.live(b3, rp.load(RT / "pk"), dry_run=True, only="pb")}
ok(len(posted) == n_before and dres["pb"]["verdict"] == "ISOLATED" and "card:ask" in dres["pb"]["deeds"] and dres["pb"]["reply"] == "Noted (dry).",
   f"server shape dry run: no POST, the seed's deeds are read, isolation judged: {dres['pb']['verdict']} {dres['pb']['deeds']}")
# busy brain (409) is waited out; a refused ask is an error, not a crash
spec_busy = rp.RunnerSpec(mode="server", endpoint=base + "/ask", status=base + "/status?id={id}", brain="brain", deeds=_deeds, poll_s=0.05, timeout_s=10, busy_wait_s=5)
(lane_root2 / "busy").mkdir(parents=True)
rb = rp.run_server(spec_busy, {"id": "busy", "agent": "beta", "input": {"text": "x"}}, lane_root2 / "busy", False)
ok(not rb.get("error") and rb["reply"] == "Card posted.", f"a 409 from the endpoint is waited out and the ask retried: {rb.get('error')}")
spec_dead = rp.RunnerSpec(mode="server", endpoint="http://127.0.0.1:9/ask", status="http://127.0.0.1:9/s?id={id}", brain="brain", poll_s=0.05, timeout_s=2, busy_wait_s=1)
(lane_root2 / "dead").mkdir(parents=True)
rd = rp.run_server(spec_dead, {"id": "dead", "agent": "beta", "input": {"text": "x"}}, lane_root2 / "dead", False)
ok(rd.get("error") and "ask refused" in rd["error"], f"an unreachable endpoint is an error verdict, not a crash: {rd.get('error')}")
dq = rp.default_question({"input": {"sender": "owner1", "role": "manager", "text": "hi", "history": ["[09:00 X] a"]}, "fixtures": {"board": {"a": 1}}})
ok(dq.startswith("[09:00 X] a") and "[FIXTURES:" in dq and dq.endswith("[from owner1 (role: manager)]\nhi"), f"default question = history, fixtures, headed text: {dq!r}")
# --limit caps the turns actually run; skips do not count
lres = rp.live(b3, rp.load(RT / "pk"), limit=1)
ran = [r["packet"] for r in lres if r["verdict"] in ("PASS", "FAIL")]
over = [r["packet"] for r in lres if "over --limit" in r.get("note", "")]
ok(len(ran) == 1 and len(over) == 2 and set(over) == {"pa", "pc"}, f"--limit 1 runs one turn and marks every later packet SKIP: ran={ran} over={over}")
srv.shutdown()
print(f"ALL {N} OK")
