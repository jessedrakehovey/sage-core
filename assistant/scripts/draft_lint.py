"""draft_lint.py - check a drafted text/email against the corrections the owner keeps making.

Ported from the property OS's scripts/comms/draft_lint.py (2026-10-06). The rules live in
assistant/data/draft_rules.json. It REPORTS only: never rewrites, never sends, never posts.
cards.post runs it on every imessage_send / email_send card: a BLOCK finding refuses the
card (the operator rewrites), warnings ride along as advice. The repeated-greeting check
reads phone_threads.json: a second "hey" to the same person on the same day is a polish
problem, so it only warns.

  python assistant/scripts/draft_lint.py --audience personal --channel text --draft-file d.txt [--to +1805...]
  echo "text" | python assistant/scripts/draft_lint.py --audience lead --channel text --draft-file -
Exit: 0 clean (info allowed), 2 warnings only, 3 at least one block, 1 usage error.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RULES_FILE = HERE.parent / "data" / "draft_rules.json"
AUDIENCES = ("lead", "couple", "vendor", "personal", "owner6")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

_GREETING_RE = re.compile(r"^\s*(?:good\s+(?:morning|afternoon|evening)|morning|afternoon|evening|hi|hello|hey)\b", re.I)


def load_rules(path: Path = RULES_FILE) -> dict:
    """The organization's rules; the engine's template when it has none yet; no rules at all before that (a draft is
    never blocked because a rules file is missing)."""
    for p in (path, HERE.parent / "templates" / "data" / path.name):
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8"))
    return {"rules": [], "limits": {}}


def audience_for(lane: str, kind: str = "", thread_class: str = "") -> str:
    """lane / comms class -> rule audience."""
    if thread_class in AUDIENCES:
        return thread_class
    lane = str(lane or "")
    if lane == "owner6":
        return "owner6"
    if lane == "wedding":
        return "lead"
    return "personal"


def _sent_earlier_today(to: str) -> list:
    """Our 'out' messages to this number earlier today (phone_threads.json). [] if unknown."""
    try:
        import phone_bridge
        th = phone_bridge.thread(to, 40)
        today = time.strftime("%Y-%m-%d")
        return [m for m in th["messages"] if m.get("dir") == "out" and str(m.get("ts", ""))[:10] == today]
    except Exception:  # noqa: BLE001
        return []


def repeat_greeting(text: str, to: str) -> list:
    if not to or not _GREETING_RE.search(str(text or "")):
        return []
    prior = _sent_earlier_today(to)
    if not prior:
        return []
    when = str(prior[-1].get("ts") or "")[11:16]
    return [{"rule": "greeting-repeated-same-day", "severity": "warn",
             "match": _GREETING_RE.search(text).group(0).strip(),
             "fix": "Already wrote to them today%s. Open with the point, not a second greeting." % ((" at " + when) if when else "")}]


def lint(text: str, audience: str, channel: str, enforcing: bool = False, rules: dict | None = None, to: str = "") -> list:
    rules = rules or load_rules()
    text = str(text or "")
    out = list(repeat_greeting(text, to)) if channel == "text" else []
    for r in rules.get("rules", []):
        aud = r.get("audience", "any")
        if aud not in ("any", audience):
            continue
        kind = r.get("kind", "forbid")
        if kind == "forbid":
            m = re.search(r["pattern"], text, re.I | re.M)
            if m:
                out.append({"rule": r["id"], "severity": r["severity"], "match": m.group(0).strip(), "fix": r["fix"]})
        elif kind == "needs_any":
            if enforcing:
                continue
            if not re.search(r["pattern"], text, re.I):
                out.append({"rule": r["id"], "severity": r["severity"], "match": "", "fix": r["fix"]})
        elif kind == "max_questions":
            n = text.count("?")
            if n > int(r.get("max", 2)):
                out.append({"rule": r["id"], "severity": r["severity"], "match": "%d questions" % n, "fix": r["fix"]})
    lim = rules.get("limits", {})
    if channel == "text":
        cap = int(lim.get("personal_text_chars" if audience == "personal" else "text_chars", 320))
        if len(text) > cap:
            out.append({"rule": "too-long", "severity": "warn", "match": "%d chars (cap %d)" % (len(text), cap),
                        "fix": "Shorten. Keep only what they need to act on."})
    else:
        cap = int(lim.get("email_words", 0))
        if cap and len(text.split()) > cap:
            out.append({"rule": "too-long", "severity": "warn", "match": "%d words (cap %d)" % (len(text.split()), cap),
                        "fix": "Shorten. Facts and one ask."})
    return out


def check_card(q: dict) -> tuple:
    """For cards.post: (blocks, warnings) for a question's exec draft, each a list of strings. ([], []) when
    the card sends nothing. Never raises."""
    try:
        ex = q.get("exec") or {}
        kind = str(ex.get("kind") or "")
        if kind not in ("imessage_send", "email_send"):
            return [], []
        p = ex.get("payload") or {}
        text = str(p.get("body") or p.get("text") or "")
        channel = "text" if kind == "imessage_send" else "email"
        aud = audience_for(q.get("lane") or "", kind, str(q.get("thread_class") or ""))
        to = str(p.get("to") or "") if channel == "text" else ""
        findings = lint(text, aud, channel, bool(q.get("enforcing")), to=to)
        fmt = lambda f: f"{f['rule']}" + (f" [{f['match']}]" if f.get("match") else "") + f": {f['fix']}"
        return ([fmt(f) for f in findings if f["severity"] == "block"],
                [fmt(f) for f in findings if f["severity"] == "warn"])
    except Exception as e:  # noqa: BLE001
        return [], [f"draft_lint crashed: {type(e).__name__}: {str(e)[:80]}"]


def main() -> int:
    ap = argparse.ArgumentParser(description="Lint a drafted message against owner1's recurring corrections.")
    ap.add_argument("--audience", required=True, choices=list(AUDIENCES))
    ap.add_argument("--channel", required=True, choices=["text", "email"])
    ap.add_argument("--draft-file", required=True, help="path to the draft body, or - for stdin")
    ap.add_argument("--enforcing", action="store_true", help="the message sets a boundary (skip the warmth check)")
    ap.add_argument("--to", default="", help="recipient number: enables the repeated-greeting check")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    try:
        text = sys.stdin.read() if a.draft_file == "-" else Path(a.draft_file).read_text(encoding="utf-8-sig")
    except OSError as exc:
        print("cannot read draft: %s" % exc, file=sys.stderr)
        return 1
    findings = lint(text, a.audience, a.channel, a.enforcing, to=a.to)
    blocks = [f for f in findings if f["severity"] == "block"]
    warns = [f for f in findings if f["severity"] == "warn"]
    if a.json:
        print(json.dumps({"clean": not findings, "blocks": len(blocks), "findings": findings}, ensure_ascii=False, indent=1))
    else:
        if not findings:
            print("DRAFT_LINT clean")
        for f in findings:
            print("%-5s %-36s %s" % (f["severity"].upper(), f["rule"], ("[" + f["match"] + "] " if f["match"] else "") + f["fix"]))
        print("TONE  wording only: re-read it as the person receiving it before carding")
    return 3 if blocks else (2 if warns else 0)


if __name__ == "__main__":
    sys.exit(main())
