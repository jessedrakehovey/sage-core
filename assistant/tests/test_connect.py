"""Offline tests for chat setup of a connection (assistant/scripts/connect.py): steps on ask, the
one-time token recognised in a message, claim routed to a handler, the token scrubbed from the
agent's records. No network; the claim handler is stubbed."""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import base64
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="wv_connect_"))
os.environ["ASSISTANT_DATA"] = str(TMP)
sys.path.insert(0, str(HERE.parent / "scripts"))
import connect  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


rows = connect.chat_rows()
check("the SimpleFIN row has chat setup", any(r["id"] == "bank-simplefin" for r in rows), [r["id"] for r in rows])

s = connect.steps("bank-simplefin")
check("steps: numbered, with the SimpleFIN link and the paste-it-here step",
      s.startswith("Here's how") and "1. " in s and "https://beta-bridge.simplefin.org/" in s and "Paste the token here" in s, s)
check("steps for an unknown connection -> ''", connect.steps("nope") == "")

for msg in ("can you set up simplefin for me", "let's connect my banks", "link my bank accounts please", "how do I set up SimpleFIN"):
    check(f"asked: {msg!r}", connect.asked(msg) == "bank-simplefin", connect.asked(msg))
for msg in ("what's my bank balance", "set up a call with example", "the bank called"):
    check(f"not asked: {msg!r}", connect.asked(msg) == "", connect.asked(msg))

# a general bank ask while a feed is down -> the Relay/Plaid steps first (the owner 2026-10-17)
ask = "Can I set up a bank account from here?"
check("the 2026-10-17 ask matches the SimpleFIN row's words", connect.asked(ask) == "bank-simplefin", connect.asked(ask))
a = connect.answer(ask, {"bank-plaid": {"state": "dead"}, "bank-simplefin": {"state": "live"}})
check("Relay feed down -> Plaid steps, then one line naming SimpleFIN",
      a.startswith("Here's how to connect Bank feed: Relay via Plaid") and "/setup/plaid" in a and 'Say "set up simplefin"' in a and "beta-bridge" not in a, a)
a = connect.answer(ask, {"bank-plaid": {"state": "live"}, "bank-simplefin": {"state": "not_configured"}})
check("SimpleFIN never set up (Relay the only bank) -> Plaid steps too", "/setup/plaid" in a and "beta-bridge" not in a, a)
a = connect.answer(ask, {"bank-plaid": {"state": "live"}, "bank-simplefin": {"state": "live"}})
check("both feeds working -> the SimpleFIN steps as before", "beta-bridge.simplefin.org" in a and "Meant a different bank" not in a, a)
a = connect.answer("how do I set up SimpleFIN", {"bank-plaid": {"state": "dead"}})
check("naming SimpleFIN always gets SimpleFIN", "beta-bridge.simplefin.org" in a and "/setup/plaid" not in a, a)
check("no status recorded -> unchanged", "beta-bridge.simplefin.org" in connect.answer(ask, {}))
check("set up plaid -> Plaid steps, no pointer line", "/setup/plaid" in connect.answer("set up plaid", {}) and "Meant a different" not in connect.answer("set up plaid", {}))
check("not a setup ask -> ''", connect.answer("what's my bank balance", {"bank-plaid": {"state": "dead"}}) == "")

token = base64.b64encode(b"https://beta-bridge.simplefin.org/simplefin/claim/aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789").decode().rstrip("=")
rid, val = connect.detect(token)
check("a bare SimpleFIN setup token is recognised", rid == "bank-simplefin" and val == token, (rid, len(val)))
rid, val = connect.detect("here you go: " + token + " thanks!")
check("a token inside a sentence is recognised", rid == "bank-simplefin" and val == token)
other = base64.b64encode(b"https://example.com/claim/" + b"x" * 40).decode()
check("an https token for another site is not SimpleFIN's", connect.detect(other) == ("", ""))
junk = base64.b64encode(b"just a long block of ordinary text that is not a url at all, really").decode()
check("random base64 is ignored", connect.detect(junk) == ("", ""))
check("ordinary long words are ignored", connect.detect("Supercalifragilisticexpialidocious" * 3) == ("", ""))

# claim: route to the handler, never log the value, report the pull
calls = []
connect.HANDLERS["simplefin"] = (lambda v: (calls.append(("claim", v)) or (True, "")), lambda: "Linked. I can see 2 accounts: Chase Checking, Amex.")
out = connect.claim("bank-simplefin", token)
check("claim routes to the handler and returns the pull sentence", calls == [("claim", token)] and out.startswith("Linked. I can see 2 accounts"), out)
connect.HANDLERS["simplefin"] = (lambda v: (False, "could not claim the token: HTTP 403"), lambda: "x")
out = connect.claim("bank-simplefin", token)
check("a failed claim says so and asks for a fresh token", "didn't take" in out and "New App" in out, out)
check("unknown connection -> honest line", "no way to store" in connect.claim("nope", token))

# scrub
agent = TMP / "agent"; agent.mkdir(parents=True, exist_ok=True)
(agent / "transcript.jsonl").write_text(json.dumps({"who": "owner1", "text": "here: " + token}) + "\n" + json.dumps({"who": "Sage", "text": "Linked."}) + "\n", encoding="utf-8")
(agent / "inbox.jsonl").write_text(json.dumps({"text": token}) + "\n", encoding="utf-8")
(agent / "other.json").write_text("{}", encoding="utf-8")
n = connect.scrub(token)
t = (agent / "transcript.jsonl").read_text(encoding="utf-8")
check("scrub rewrites every record holding the token", n == 2 and token not in t and connect.SCRUBBED in t and token not in (agent / "inbox.jsonl").read_text(encoding="utf-8"), (n, t[:120]))
check("scrub of a short value is refused", connect.scrub("abc") == 0)

hint = connect.chat_hint()
check("the CONNECTIONS hint names the chat-settable connection", "Chat setup available" in hint and "simplefin" in hint, hint)

# the morning brief's CONNECTIONS sentence (plan 2026-10-18 section 2): what is NOT connected + how to fix it, config-driven
status = {"mail-personal": {"state": "not_configured"}, "calendar-personal": {"state": "not_configured"}, "google-owner6": {"state": "not_configured"},
          "bank-plaid": {"state": "dead"}, "next-insurance": {"state": "not_configured"}, "mail-example": {"state": "live"}, "honeybook": {"state": "live"}}
watch = ["mail-personal", "calendar-personal", "google-owner6", "bank-plaid", "next-insurance", "mail-example"]
line = connect.brief_line(status, watch)
check("one sentence, starts CONNECTIONS:, ends with a full stop, no line breaks", line.startswith("CONNECTIONS: ") and line.endswith(".") and "\n" not in line, line)
check("names every watched connection that is not working, in config order, never a live one",
      [n for n in ("your personal Gmail (owner4@example.invalid)", "your personal calendar", "owner6's Google account", "the Relay bank feed (Plaid)", "the NEXT Insurance login") if n in line]
      == ["your personal Gmail (owner4@example.invalid)", "your personal calendar", "owner6's Google account", "the Relay bank feed (Plaid)", "the NEXT Insurance login"]
      and "example.invalid" not in line and "HoneyBook" not in line, line)
check("each one carries its fix: the Setup-page button or the chat phrase",
      "Setup page: Enter app password" in line and "Setup page: Enter calendar link" in line and "Setup page: Run setup (she signs in)" in line
      and 'say "set up plaid" here' in line and "Setup page: Sign in" in line, line)
check("dead vs never set up are worded apart", "the Relay bank feed (Plaid) (stopped working" in line and "the NEXT Insurance login (not connected yet" in line, line)
check("plain names come from the registry's brief_name, not the technical name", "Mail: owner1" not in line and "Calendar (read)" not in line, line)
check("all working -> '' (the brief omits the line)", connect.brief_line({k: {"state": "live"} for k in watch}, watch) == "")
check("a watched id missing from the status file counts as not configured", "your personal Gmail" in connect.brief_line({}, ["mail-personal"]))
check("an unknown id is skipped quietly", connect.brief_line({"zzz": {"state": "dead"}}, ["zzz"]) == "")
cfg_watch = json.loads((HERE.parent / "config.json").read_text(encoding="utf-8"))["brief"]["connections"]
check("config.json -> brief.connections holds the five from the plan", cfg_watch == ["mail-personal", "calendar-personal", "google-owner6", "bank-plaid", "next-insurance"], cfg_watch)
import subprocess as _sp
r = _sp.run([sys.executable, str(HERE.parent / "scripts" / "connect.py"), "brief"], capture_output=True, text=True, encoding="utf-8", cwd=str(HERE.parent.parent))
check("connect.py brief (CLI) prints the sentence or nothing, never crashes", r.returncode == 0 and (r.stdout.strip() == "" or r.stdout.startswith("CONNECTIONS: ")), (r.returncode, r.stdout[:120], r.stderr[-200:]))

# setup_creds accepts the token on stdin (argument parsing only; no claim without network)
import subprocess
r = subprocess.run([sys.executable, str(HERE.parent / "scripts" / "setup_creds.py"), "money", "simplefin", "--stdin"], input="\n", capture_output=True, text=True, cwd=str(HERE.parent.parent))
check("setup_creds --stdin with an empty line exits 'nothing entered'", "nothing entered" in (r.stdout + r.stderr) or r.returncode != 0, (r.stdout + r.stderr)[-200:])

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)} (scratch {TMP})")
sys.exit(1 if FAILS else 0)
