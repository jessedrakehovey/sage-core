"""test_rulings.py - rulings.find pins a card down or stays quiet (decision study 2026-10-30, ported from the
property OS the same day: 220 ruling hits on 100 sampled cards, 4 relevant, nearly all from GENERIC card words and
from any 9+ letter word counting as a phrase).

Positive controls: a specific phrase, a record number, two specific words. Negatives: the top false firers -- a
lone long word ("electrical", "completion", "scheduled"), a pair of GENERIC words ("work order" + "photos",
"email" + "text"), a unit code. weak_rulings() is empty for a well-cut file and names the ruling that can never
match; `add` refuses such a keyword set. Scratch RULINGS_FILE; never the live store.
  python assistant/tests/test_rulings.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="rulings_test_"))
os.environ["RULINGS_FILE"] = str(TMP / "rulings.json")
sys.path.insert(0, str(HERE.parent / "scripts"))
import rulings  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def R(rid, kws, lane=""):
    return {"id": rid, "status": "active", "date": "2026-10-30", "lane": lane, "keywords": kws,
            "ruling": f"ruling {rid}", "quote": f"quote {rid}"}


D = {"rulings": [
    R("electrical-closes-in-house", ["electrical close", "electrical work order", "panel"]),
    R("record-191660", ["191660", "the fence job"]),
    R("two-words", ["generator", "propane"]),
    R("inbox-choice", ["inbox", "which email", "which account"], lane="personal"),
    R("retired-one", ["retired phrase here"]),
]}
D["rulings"][-1]["status"] = "retired"

# ---------------------------------------------------------------- keyword classes
check("a multi-word phrase is strong", rulings.is_strong("electrical close"))
check("a hyphenated record number is strong", rulings.is_strong("181751-1"))
check("a bare record number is strong", rulings.is_strong("191660"))
check("a unit code is not strong", not rulings.is_strong("B43"))
check("a lone long word is not strong", not rulings.is_strong("electrical") and not rulings.is_strong("completion"))
check("a GENERIC phrase is never strong", not rulings.is_strong("work order") and not rulings.is_strong("follow-up"))
check("GENERIC words are not specific", not rulings.is_specific("Email") and not rulings.is_specific("photos"))
check("an ordinary word is specific", rulings.is_specific("generator"))

# ---------------------------------------------------------------- positive controls
ids = lambda text, **k: [h["id"] for h in rulings.find(text, D, **k)]  # noqa: E731
check("specific phrase matches", ids("Close the electrical close on B43 with photos?") == ["electrical-closes-in-house"])
check("record number alone matches", ids("WO 191660: approve the invoice?") == ["record-191660"])
check("two distinct specific words match", "two-words" in ids("The generator needs propane before Saturday"))
check("GENERIC hits still raise the score of a matched ruling",
      rulings.find("electrical close, panel photos", D)[0]["score"] == 2)
check("lane: a lane ruling matches its own lane and no lane", "inbox-choice" in ids("which email do I reply from?", lane="personal")
      and "inbox-choice" in ids("which email do I reply from?"))
check("lane: a lane ruling stays out of another lane", "inbox-choice" not in ids("which email do I reply from?", lane="money"))
check("a retired ruling never matches", not ids("retired phrase here"))

# ---------------------------------------------------------------- the top false firers
check("a lone long word does not match", not ids("electrical work on the panel is scheduled for completion"))
check("the same specific word twice is one word", not ids("propane, propane, propane"))
check("work order + photos (every WO card) does not match",
      not rulings.find("work order 12 photos uploaded; close it?", {"rulings": [R("g", ["work order", "photos", "close"])]}))
check("email + text (every reply card) does not match",
      not rulings.find("reply by email or text?", {"rulings": [R("g", ["email", "text", "reply"])]}))
check("one GENERIC + one specific word does not match",
      not rulings.find("send the panel photos", {"rulings": [R("g", ["panel", "photos"])]}))
check("a unit code alone does not match", not rulings.find("B43 again", {"rulings": [R("u", ["B43"])]}))
check("a unit code plus a specific word does match", bool(rulings.find("B43 generator", {"rulings": [R("u", ["B43", "generator"])]})))

# ---------------------------------------------------------------- weak rulings
check("weak_rulings(D) == []", rulings.weak_rulings(D) == [])
W = {"rulings": [R("lone-word", ["electrical"]), R("generic-pair", ["work order", "photos"]), R("fine", ["electrical close"]),
                 R("retired-weak", ["note"])]}
W["rulings"][-1]["status"] = "retired"
check("weak_rulings names the active rulings that can never match", rulings.weak_rulings(W) == ["lone-word", "generic-pair"])

# ---------------------------------------------------------------- the CLI
def cli(*args):
    r = subprocess.run([sys.executable, str(HERE.parent / "scripts" / "rulings.py"), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=dict(os.environ))
    return r.returncode, (r.stdout or "") + (r.stderr or "")


(TMP / "rulings.json").write_text(json.dumps(W), encoding="utf-8")
rc, out = cli("add", "--id", "weak-add", "--ruling", "x", "--quote", "y", "--keywords", "electrical, photos")
check("add refuses a keyword set that can never match", rc == 1 and "can never match" in out, out[-200:])
rc, out = cli("add", "--id", "good-add", "--ruling", "x", "--quote", "y", "--keywords", "electrical close")
check("add takes a specific phrase", rc == 0 and "ADDED good-add" in out, out[-200:])
rc, out = cli("list")
check("list flags weak rulings", rc == 0 and out.count("[WEAK KEYWORDS: can never match]") == 2 and "good-add" in out, out[-400:])
rc, out = cli("find", "close the electrical close")
check("find exit 0 on a match", rc == 0 and "RULING" in out, out[-200:])
rc, out = cli("find", "electrical")
check("find exit 4 with no match", rc == 4 and "NO_RULING" in out, out[-200:])

# ---------------------------------------------------------------- ONE RULE STORE: Sage's rules + lane lessons
rules = [{"id": "r7", "rule": "Never re-send the finishing touches file once the couple opened it", "why": "owner1 said so",
          "added": "2026-10-20 09:00", "active": True},
         {"id": "r8", "rule": "Retired: always text the florist about centerpieces", "added": "2026-10-01", "active": False},
         {"id": "r9", "rule": "Keep replies short and warm", "added": "2026-10-02", "active": True}]
rules += [{"id": "h%d" % n, "rule": "House rule %d about mission control pages" % n, "added": "2026-10-03", "active": True}
          for n in range(4)]
(TMP / "rules.json").write_text(json.dumps({"rules": rules}), encoding="utf-8")
(TMP / "learn").mkdir()
(TMP / "learn" / "wedding.md").write_text(
    "# wedding lane learnings\n\n## Lessons\n"
    "- 2026-10-07 — [raw edit on 'Text reply'] owner1 changed the draft to: drone footage soon (q697c0f6ab1c1)\n"
    "- 2026-10-09 — Mention the drone footage only when the package includes it (qabc123def456)\n", encoding="utf-8")
rulings.AGENT_RULES = TMP / "rules.json"
rulings.LEARN_DIR = TMP / "learn"
os.environ["AGENT_RULES_FILE"] = str(TMP / "rules.json")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learn")

strong, words = rulings.rule_terms("Never re-send the finishing touches file once the couple opened it")
check("rule_terms: two consecutive specific words form a phrase", "finishing touches" in strong, strong)
check("rule_terms: stopwords never form a phrase", not any(p.startswith("never") or p.endswith(" the") for p in strong), strong)
aid = lambda text, **k: [h["id"] for h in rulings.find_agent_rules(text, **k)]  # noqa: E731
check("an active Sage rule matches on its phrase", aid("Send the Finishing Touches file to Kim and Ray?") == ["sage r7"],
      rulings.find_agent_rules("Send the Finishing Touches file to Kim and Ray?"))
check("a retired Sage rule never matches", not aid("text the florist about centerpieces"))
check("a lone specific word does not match", not aid("which file should I attach?"))
check("a house noun in 4+ rules never matches alone", not aid("open the mission control pages"))
check("a lane lesson matches, raw edits are skipped", aid("Should the email mention drone footage?") == ["wedding L2"])
check("a lane lesson stays in its lane", not aid("Should the email mention drone footage?", lane="personal"))
h = rulings.find_agent_rules("Send the finishing touches file?")[0]
check("an agent hit carries source, date and why", h["source"] == "sage" and h["date"] == "2026-10-20" and h["why"] == "owner1 said so", h)
rc, out = cli("find", "Send the finishing touches file?")
check("CLI find also searches Sage's rules", rc == 0 and "RULE sage r7" in out, out[-300:])
rc, out = cli("find", "Send the finishing touches file?", "--no-agents")
check("CLI --no-agents searches rulings.json only", rc == 4 and "NO_RULING" in out, out[-300:])

print(f"\n{len(FAILS)} failures" if FAILS else "\nALL OK")
sys.exit(1 if FAILS else 0)
