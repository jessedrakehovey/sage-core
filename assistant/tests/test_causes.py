"""Offline tests for assistant/agent/causes.py (Seed E, causes: label first, learn later). The vocabulary is data with
a ceiling of six labels plus skip; a label about the people the organization serves is dropped at load and refused
by the validator; a cause is written only from a human source (tap / typed), never from a model; skip writes
nothing; a typed free text is kept for the shadow test and clustered by keyword hints; the coverage read-back
counts closes with a cause, free texts that cluster, items with nobody to chase; the words are plain; and the
engine file names no organization.
   python assistant/tests/test_causes.py
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
import causes as cz  # noqa: E402

N = 0


def ok(cond, msg):
    global N
    N += 1
    if not cond:
        print(f"FAIL {N}: {msg}")
        sys.exit(1)
    print(f"ok {N}: {msg}")


def raises(fn, *a, **k):
    try:
        fn(*a, **k)
    except cz.CauseError as e:
        return str(e)
    return None


NOW = datetime(2026, 11, 2, 9, 30)
TMP = Path(tempfile.mkdtemp(prefix="causes_"))

# --- the vocabulary as data
V = cz.load_vocab()
ok(V["labels"] == ["vendor_no_reply", "vendor_no_show", "parts_wait", "approval_waited", "key_missing", "wrong_channel"],
   "the default vocabulary is the plan's six labels in order")
ok(V["skip"] == "skip" and "resident" in V["never"] and "household" in V["never"], "skip and the never stems are there by default")
tv = cz.load_vocab(ROOT / "agent" / "cause_vocab.json")
ok(tv["labels"] == V["labels"] and tv["words"]["parts_wait"] == "Waited on parts" and tv["refused"] == [],
   "the template file loads: same six labels, card words, nothing refused")
p = TMP / "v.json"
p.write_text(json.dumps({"labels": ["vendor_no_reply", "resident_unreachable", "parts wait", "Tenant-Away", "x!", "aa", "bb", "cc", "dd", "ee", "ff"],
                         "never": ["guest"], "words": {"parts_wait": "Waiting on a part"}}), encoding="utf-8")
pv = cz.load_vocab(p)
ok("resident_unreachable" not in pv["labels"] and "tenant_away" not in pv["labels"],
   "a people-attributed label is dropped at load, whatever the case or the dash")
ok([r["label"] for r in pv["refused"]][:3] == ["resident_unreachable", "tenant_away", "x!"], f"the refusals are listed with the label: {pv['refused'][:3]}")
ok(pv["labels"][:2] == ["vendor_no_reply", "parts_wait"] and len(pv["labels"]) == 6, f"spaces normalize to underscores; six is the ceiling: {pv['labels']}")
ok(any(r["why"] == "beyond the sixth label" for r in pv["refused"]), "the seventh label is refused as beyond the sixth")
ok("guest" in pv["never"] and "resident" in pv["never"], "a pack may ADD never stems; it cannot remove the default ones")
ok(pv["words"]["parts_wait"] == "Waiting on a part" and pv["words"]["skip"] == "skipped", "card words merge over the defaults; skip has words")
ok(cz.load_vocab(TMP / "missing.json")["labels"] == V["labels"], "an unreadable file falls back to the defaults")

# --- the validator
ok(cz.validate("parts_wait", V) == (True, ""), "a vocabulary label validates")
ok(cz.validate("Parts Wait", V)[0] is True, "case and spaces are normalized before the check")
ok(cz.validate("skip", V) == (True, ""), "skip validates (it writes nothing)")
okk, why = cz.validate("vendor_lazy", V)
ok(okk is False and "not in the vocabulary" in why and "vendor_no_reply" in why, f"a label outside the vocabulary is refused in words: {why}")
okk, why = cz.validate("resident_unreachable", V)
ok(okk is False and "never about the people served" in why, f"resident_unreachable is refused by the validator, not only at load: {why}")
ok(cz.validate("household_no_access", V)[0] is False and cz.validate("tenant", V)[0] is False, "any people-attributed label is refused")
ok(cz.validate("", V)[0] is False and cz.validate(None, V)[0] is False and cz.validate({"cause": "x"}, V)[0] is False,
   "empty / None / a dict are refused")
ok(cz.check_text("the vendor went quiet for a week", V) == "" and "people served" in cz.check_text("the tenant was never home", V),
   "a free text about the people served is refused; one about the vendor is allowed")

# --- label_close: a human choice only
it = {"id": "x1", "status": "answered", "vendor": "a vendor"}
cz.label_close(it, "vendor_no_reply", "tap", NOW, V)
ok(it["cause"] == "vendor_no_reply" and it["cause_source"] == "tap" and it["cause_ts"] == "2026-11-02T09:30:00",
   "a tapped label writes cause / cause_source / cause_ts")
e = raises(cz.label_close, {"status": "answered"}, "vendor_no_reply", "model", NOW, V)
ok(e and "human choice" in e, f"a cause from a model is refused: {e}")
e = raises(cz.label_close, {"status": "answered"}, "vendor_no_reply", "", NOW, V)
ok(e and "human choice" in e, "a cause with no source is refused")
e = raises(cz.label_close, {"status": "answered"}, "vendor_no_reply", "inferred", NOW, V)
ok(e and "human choice" in e, "a cause marked inferred is refused")
for src in ("dream", "agent", "auto", "brain"):
    ok(raises(cz.label_close, {}, "parts_wait", src, NOW, V) is not None, f"source '{src}' is refused")
it2 = {"id": "x2", "status": "parked"}
same = cz.label_close(it2, "skip", "tap", NOW, V)
ok(same is it2 and "cause" not in it2 and "cause_source" not in it2 and "cause_ts" not in it2, "skip writes nothing at all")
e = raises(cz.label_close, {"status": "answered"}, "vendor_lazy", "tap", NOW, V)
ok(e and "not in the vocabulary" in e, "a tapped label outside the vocabulary is refused")
e = raises(cz.label_close, {"status": "answered"}, "resident_unreachable", "tap", NOW, V)
ok(e and "people served" in e, "a tapped people-attributed label is refused")
e = raises(cz.label_close, {"status": "answered"}, "resident_unreachable", "typed", NOW, V)
ok(e and "people served" in e, "a TYPED people-attributed label is refused too (no path writes it)")
it3 = {"id": "x3", "status": "answered"}
cz.label_close(it3, "waited two weeks on the part", "typed", NOW, V)
ok(it3.get("cause_text") == "waited two weeks on the part" and "cause" not in it3 and it3["cause_source"] == "typed"
   and it3["cause_text_ts"] == "2026-11-02T09:30:00", "a typed free text is kept as cause_text, never as cause")
e = raises(cz.label_close, {"status": "answered"}, "the tenant was never home", "typed", NOW, V)
ok(e and "people served" in e, "a typed free text about the people served is refused")
e = raises(cz.label_close, {"status": "answered"}, "the household would not open", "typed", NOW, V)
ok(e is not None, "household in a free text is refused")
e = raises(cz.label_close, {"status": "answered"}, "   ", "typed", NOW, V)
ok(e is not None, "a blank text is refused")
cz.label_close(it3, "Parts wait", "typed", NOW, V)
ok(it3["cause"] == "parts_wait" and "cause_text" not in it3, "a label typed later replaces the free text")
cz.label_close(it3, "some other words", "typed", NOW, V)
ok(it3["cause"] == "parts_wait" and "cause_text" not in it3, "a free text never overwrites a label already chosen")
long = "x" * 500
it4 = cz.label_close({"status": "answered"}, long, "typed", NOW, V)
ok(len(it4["cause_text"]) == cz.MAX_TEXT, "a free text is trimmed to MAX_TEXT")
ok(raises(cz.label_close, {}, ["parts_wait"], "tap", NOW, V) is not None, "a list is not a cause")
ok(raises(cz.label_close, "not a dict", "parts_wait", "tap", NOW, V) is not None, "a cause goes on a dict")
it5 = cz.label_close({"status": "awaiting"}, "key_missing", "tap", "2026-11-02T08:00", V)
ok(it5["cause_ts"] == "2026-11-02T08:00", "a string stamp is written as given")
ok(cz.label_close({"status": "answered"}, "vendor_no_show", "TAP ", NOW, V)["cause_source"] == "tap", "the source is trimmed and lowered")

# --- parse_typed
ok(cz.parse_typed("stalled: waited on the part") == "waited on the part", "'stalled:' is the typed marker")
ok(cz.parse_typed("Cause - vendor no reply") == "vendor no reply", "'cause -' works too")
ok(cz.parse_typed("why: he never came") == "he never came" and cz.parse_typed("reason: parts") == "parts", "why / reason markers")
ok(cz.parse_typed("the cause of all this is me") == "" and cz.parse_typed("Done, thanks") == "", "a reply that only mentions the word is not a cause")
ok(cz.parse_typed(None) == "" and cz.parse_typed("stalled:") == "", "None / an empty marker read as nothing")

# --- clustering free text into the vocabulary (keyword hints as data)
ok(cz.cluster("vendor no reply", V) == "vendor_no_reply", "a label in words clusters to itself")
ok(cz.cluster("he never answered my texts", V) == "vendor_no_reply", "never answered -> vendor_no_reply")
ok(cz.cluster("they didn't show on Tuesday", V) == "vendor_no_show", "didn't show -> vendor_no_show")
ok(cz.cluster("waiting on the part from the supplier, backorder", V) == "parts_wait", "part / backorder -> parts_wait")
ok(cz.cluster("sat on my desk waiting for approval", V) == "approval_waited", "approval -> approval_waited")
ok(cz.cluster("nobody had a key, couldn't get in", V) == "key_missing", "key -> key_missing")
ok(cz.cluster("I asked on the wrong number", V) == "wrong_channel", "wrong number -> wrong_channel")
ok(cz.cluster("weather", V) == "" and cz.cluster("", V) == "", "nothing matches -> ''")
ok(cz.cluster("partly my fault", V) == "", "a hint matches whole words only (partly is not part)")

# --- coverage
items = {
    "a": {"status": "answered", "vendor": "v1", "cause": "vendor_no_reply", "cause_source": "tap"},
    "b": {"status": "answered", "vendor": "v1", "cause": "vendor_no_reply", "cause_source": "tap"},
    "c": {"status": "parked", "vendor": "v2", "cause_text": "never answered me", "cause_source": "typed"},
    "d": {"status": "answered", "vendor": "", "cause_text": "weather", "cause_source": "typed"},
    "e": {"status": "answered", "vendor": None},
    "f": {"status": "settled", "vendor": "v3"},
    "g": {"status": "awaiting", "vendor": "v3", "cause": "parts_wait", "cause_source": "tap"},
    "h": {"status": "awaiting", "vendor": "v3"},
    "i": {"status": "answered", "vendor": "v4", "cause": "resident_unreachable", "cause_source": "tap"},
}
rep = cz.coverage(items, V)
ok(rep["items"] == 9 and rep["closes"] == 7, f"nine items, seven closes: {rep['closes']}")
ok(rep["closes_with_cause"] == 3 and rep["closes_with_text"] == 2 and rep["closes_without"] == 2, "labels / texts / none counted")
ok(rep["by_label"] == {"vendor_no_reply": 2, "resident_unreachable": 1} and rep["by_source"] == {"tap": 3}, f"by label / by source: {rep['by_label']}")
ok(rep["clustered"] == 1 and rep["unclustered"] == 1 and rep["free_texts"][0]["reads_like"] == "vendor_no_reply", "free texts clustered by the hints")
ok(rep["no_who"] == 2 and rep["no_who_with_cause"] == 1, "closes with nobody to chase, and how many of those carry a cause")
ok(rep["open_with_cause"] == 1, "an open item with a cause is counted apart")
ok(rep["invalid"] == [{"cause": "resident_unreachable", "why": cz.validate("resident_unreachable", V)[1]}],
   "a cause outside the vocabulary that reached the file is reported, never counted as fine")
ok(rep["coverage_pct"] == 71 and rep["digest_bar_pct"] == 60 and rep["digest_bar_met"] is True, f"coverage 5 of 7 = 71 percent, over the bar: {rep['coverage_pct']}")
words = cz.coverage_words(rep)
ok(words.startswith("5 of 7 closes carry a cause (71 percent): 3 as a label, 2 as free text."), f"plain words: {words}")
ok("Labels: vendor_no_reply 2, resident_unreachable 1." in words and "Of the 2 free texts, 1 read like a vocabulary label and 1 do not." in words, words)
ok("Not clustering: 'weather'." in words and "2 closes had no vendor to chase; 1 of those carry a cause." in words, words)
ok("outside the vocabulary (should not happen): resident_unreachable" in words and "1 open item already carry a cause" in words, words)
ok(words.endswith("The digest's bar is 60 percent coverage: reached."), words)
ok(not re.search(r"\d+\.\d+|confidence|probab", words), "no decimal, no confidence word in the read-back")
empty = cz.coverage({}, V)
ok(empty["closes"] == 0 and empty["coverage_pct"] == 0 and empty["digest_bar_met"] is False, "no items: zero, bar not met")
ok(cz.coverage_words(empty) == "No closed chases yet, so nothing to read back.", "no closes: one plain sentence")
low = cz.coverage([{"status": "answered", "vendor": "v"}, {"status": "answered", "vendor": "v", "cause": "parts_wait"}], V)
ok(low["coverage_pct"] == 50 and low["digest_bar_met"] is False and cz.coverage_words(low).endswith("not reached yet."), "under the bar reads 'not reached yet'")
ok(cz.coverage([{"status": "done", "who": "", "cause": "parts_wait"}], V, closed_statuses=("done",), who_key="who")["no_who_with_cause"] == 1,
   "closed statuses and the who field are the binding's")

# --- CLI
sp = TMP / "state.json"
sp.write_text(json.dumps({"items": items}), encoding="utf-8")
import contextlib
import io
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = cz.main(["coverage", str(sp)])
out = buf.getvalue()
ok(rc == 0 and out.startswith("5 of 7 closes carry a cause") and '"coverage_pct": 71' in out, "CLI prints the words then the counts")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = cz.main(["coverage", str(sp), "--closed", "answered", "--who", "vendor"])
ok(rc == 0 and buf.getvalue().startswith("4 of 5 closes"), "--closed narrows the close statuses")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = cz.main(["nope"])
ok(rc == 2, "no subcommand prints the doc and exits 2")

# --- the engine names no organization
src = (ROOT / "agent" / "causes.py").read_text(encoding="utf-8")
ok(not re.search(r"\b(unit|work order|property|apartment)\b", src, re.I), "no property vocabulary in the engine")
ok(not re.search(r"@|https?://", src), "no address, no URL in the engine")
ok("import " not in src.split("from pathlib import Path")[1].split("HUMAN_SOURCES")[0], "the engine imports only the standard library")
print(f"ALL {N} OK")
