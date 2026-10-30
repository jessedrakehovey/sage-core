"""Offline tests for the template + self-improvement pieces (2026-10-21, Phase 4): nothing in the
agent's code, tool descriptions or generic prompt names this organization or its owners; owner words
for the safety regexes come from people.json; the nightly merge folds a dream-written duplicate into
an owner's rule and never retires an owner's; /health renders without a model.
   python assistant/tests/test_template.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_template_"))
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["RULINGS_FILE"] = str(ROOT / "data" / "rulings.json")
os.environ["DOER_DATA"] = str(TMP / "doer")
os.environ["AGENT_STATE"] = str(TMP / "agent_state")
os.environ["AGENT_DATA"] = str(TMP / "agent_data")
os.environ["AGENT_SUGGESTIONS"] = str(TMP / "sugg.md")
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "agent"))
import core  # noqa: E402
import bridge  # noqa: E402
import dream  # noqa: E402
import gmcp  # noqa: E402

N = 0


def ok(cond, msg):
    global N
    N += 1
    if not cond:
        print(f"FAIL {N}: {msg}")
        sys.exit(1)
    print(f"ok {N}: {msg}")


def _org_words(root):
    """This organization's own name and people (org.json, people.json): what a generic file must never carry."""
    out = set()
    try:
        n = str(json.loads((root / "org.json").read_text(encoding="utf-8-sig")).get("name") or "")
        out |= {w for w in [n, n.split(" ")[0]] if len(w) >= 4}
    except (OSError, ValueError):
        pass
    try:
        for k, u in (json.loads((root / "agent" / "people.json").read_text(encoding="utf-8-sig")).get("users") or {}).items():
            out |= {w for w in (k, str(u.get("name") or "").split(" ")[0]) if len(w) >= 3}
    except (OSError, ValueError):
        pass
    return sorted(out, key=len, reverse=True)


ORG_WORDS = re.compile(r"\b(?:" + "|".join(map(re.escape, _org_words(ROOT) + ["the CRM"])) + r")\b", re.I)

# ---------------------------------------------------------------- 1. the generic prompt and the tool descriptions carry no organization
gen = (ROOT / "agent" / "templates" / "prompt.generic.md").read_text(encoding="utf-8")
ok(not ORG_WORDS.search(gen), "the generic prompt names no organization, service or owner")
tools = json.dumps(gmcp.TOOLS)
ok(not re.search(r"\b(?:" + "|".join(map(re.escape, [w for w in _org_words(ROOT) if w.lower() not in {k.lower() for k in core.people().get("users") or {}} and w.lower() not in {str(u.get("name") or "").split(" ")[0].lower() for u in (core.people().get("users") or {}).values()}] + ["the CRM"])) + r")\b", tools, re.I) and set(gmcp.OWNERS) == set(core.owners()),
   "no tool description names the organization or a service; the owner enum is the owners (role owner) from people.json, never staff")

# ---------------------------------------------------------------- 2. the code carries no owner literals on its working lines
bad = []
for f in sorted((ROOT / "agent").glob("*.py")):
    for i, ln in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
        code = ln.split("#", 1)[0]
        if '"dm:owner1"' in code or "'dm:owner1'" in code:
            bad.append(f"{f.name}:{i} {ln.strip()[:80]}")
        if re.search(r"\(\?:owner1\|owner6", code):
            bad.append(f"{f.name}:{i} {ln.strip()[:80]}")
ok(not bad, "no 'dm:owner1' fallback and no owner names inside a safety regex in agent code: " + "; ".join(bad[:3]))
ok(core._OWNER_WORDS and all(w.isalnum() for w in core._OWNER_WORDS) and core.first_owner() == next(iter(core.people()["users"])),
   "owner words and the fallback owner come from people.json")
ok(core.is_protected("send the proposal without " + core._OWNER_WORDS[0] + " looking"), "the protected-core regex knows the owners by their real names")

# ---------------------------------------------------------------- 3. the nightly merge folds duplicates, never an owner's rule
import pending  # noqa: E402
# rewordings core.similar (0.7 word overlap) lets through at add time but pending.similar_text catches: the
# real r33/r44 and r28/r45 shapes of 2026-10-21, written straight into the store the way the dream's adds land
pairs = [
    ("Craft advice about directing couples, shooting and editing gives specific professional techniques with how and why they work and where they come from",
     "owner1", "Craft advice on directing couples, shooting or editing must name specific professional techniques and say where the techniques come from, never generic tips for anyone", "sage-dream"),
    ("When an owner asks about a couple's price, package or discount, read the pricing sheet, the quiz lead record and the proposal and quote the real numbers",
     "sage-dream", "A question about a couple's price or package gets the real numbers from the pricing sheet, the quiz record and the proposal, never a template list price or a guess", "owner1"),
]
d = core._rules()
for i, (ra, ba, rb, bb) in enumerate(pairs):
    for j, (rule, by) in enumerate(((ra, ba), (rb, bb))):
        d["rules"].append({"id": f"r{d['next_id']}", "rule": rule, "why": "test", "by": by, "added": f"2026-10-2{i}  0{j}:00", "last": "", "seen": 1, "active": True})
        d["next_id"] = int(d["next_id"]) + 1
core.save(core.RULES, d)
ok(all(pending.similar_text(a, b) and not core.similar(a, b) for a, _, b, _ in pairs), "the fixture pairs are the kind the add-time fold misses")
before = {r["id"]: r for r in core.rules_active()}
folds = dream.merge_rules()
after = {r["id"]: r for r in core.rules_active()}
owner_ids = {rid for rid, r in before.items() if r.get("by") == "owner1"}
ok(len(folds) == 2, f"two near-duplicate pairs folded ({len(folds)})")
ok(owner_ids <= set(after), "every owner-taught rule is still active")
ok(all(before[f["folded"]].get("by") == "sage-dream" for f in folds), "only dream-written rules were folded")
ok(dream.merge_rules() == [], "a second pass has nothing left to fold")
ch = [c for c in core.rows(core.CHANGES) if c.get("kind") == "rule_retire"]
ok(len(ch) >= 2 and all(c["undo"]["op"] == "rule_restore" for c in ch[-2:]), "each fold is a ledgered, undoable retirement")

# ---------------------------------------------------------------- 4. /health is zero-LLM and plain
owner1 = core.person_for(handle="owner1")
h = bridge.command("/health", owner1)
ok(isinstance(h, str) and h.endswith(".") and "open card" in h and "SUGGEST" not in h and "\n- " not in h, "/health renders a plain status line without a model turn")
ok("/health" in bridge.command("/help", owner1), "/help lists /health")
s = bridge.command("/setup", owner1)
ok("/setup" in s and "button" in s.lower(), "/setup names the Setup page link for the phone")

print(f"\nall {N} checks passed")
