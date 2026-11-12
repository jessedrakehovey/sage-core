"""The chat-first rule, kept by a test (agent/chatfirst.py, 2026-11-04): the agent is 100% inside Google Chat, a portal
into the PC. No owner-facing line, card, dialog or prompt may tell an owner to do something at the computer.

Three parts: the rule itself (what it flags, what it leaves alone, what clean() does), the doors (voice.polish, the
doer's result line, a card's details), and a sweep of every owner-facing source in the engine: the cards the code
builds, the lines the doer says, the setup runner's results from chat, the registry words shown on cards, the prompts.
Scratch stores; no browser, no Google.   python assistant/tests/test_chat_first.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))   # never the live ~/.claude logs
import json
import os
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_chatfirst_"))
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["AGENT_STATE"] = str(TMP / "agent_state")
os.environ["AGENT_DATA"] = str(TMP / "agent_data")
os.environ["DOER_DATA"] = str(TMP / "doer")
os.environ["DOER_STATUS"] = str(TMP / "doer_status.md")
os.environ["AGENT_HOME_SPACE"] = ""
os.environ["STARTUP_STATE"] = str(TMP / "startup_state.json")
os.environ["HOME_TAB_CACHE"] = str(TMP / "home_cache.json")
os.environ["ASSISTANT_CHANNEL"] = "null"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "doer"))
sys.path.insert(0, str(ROOT / "agent"))
import chatfirst  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def clean_of(text) -> list:
    return chatfirst.violations(text if isinstance(text, str) else json.dumps(text, ensure_ascii=False))


# ------------------------------------------------------------------ 1. the rule
BAD = {
    "terminal": "Open a terminal and finish it there.",
    "command": "Run python assistant/doer/vault.py add to store it.",
    "at_the_pc": "Sign in on the PC, then tap Check.",
    "pc_screen": "It is open on your screen now, maximized on your main monitor.",
    "pc_window": "A sign-in window will be up until you sign in.",
    "setup_page": "Its button is on the Setup page.",
    "mission_control": "Upload the file on Mission Control instead.",
    "by_hand": "The owner has to sign in by hand.",
    "windows": "Settings, Time & language, turn on Set time automatically.",
    "config": "I will pick it up tomorrow, or raise doer.runs_per_day_max.",
}
check("every rule flags its own kind of PC-pointing line", all(name in chatfirst.violations(t) for name, t in BAD.items()),
      repr({n: chatfirst.violations(t) for n, t in BAD.items() if n not in chatfirst.violations(t)}))
GOOD = ["Tap Enter login: it opens a form on top of chat and goes straight to my vault.",
        "Done: the cart is built. Control PC shows it if you want to look.",
        "CPU 12% · 410 GB free · browser closed",
        "The agent runs on a PC at home and answers here.",
        "Reply here with the code and I'll carry on.",
        "I sign back in from my vault; a login I am missing comes to you as a card here.",
        "Say 'finish setting up' and the card comes up."]
check("chat-first lines pass, 'Control PC' is a window not a step, a fact about the PC is not an instruction",
      not any(chatfirst.violations(t) for t in GOOD), repr([(t, chatfirst.violations(t)) for t in GOOD if chatfirst.violations(t)]))
c = chatfirst.clean("I could not finish the upload. Open a terminal and run python x.py --go. The file is safe.")
check("clean drops the PC sentence and keeps the rest, with no stock line tacked on (2026-11-11)", c == "I could not finish the upload. The file is safe." and not chatfirst.violations(c), c)
check("... the stock line stands in only when nothing else is left", chatfirst.clean("Open a terminal and run python x.py --go.") == chatfirst.HANDLED)
check("clean leaves a clean line untouched", chatfirst.clean(GOOD[0]) == GOOD[0])
check("a lead-in goes with the step it introduced", "from your phone:" not in chatfirst.clean("Here is how from your phone: open a terminal on the PC."))

# ------------------------------------------------------------------ 2. the doors
import voice  # noqa: E402
voice._ledger = lambda *a, **k: None
check("voice.polish (every code line to Chat, and the model's own reply) applies the rule",
      not chatfirst.violations(voice.polish("That failed. Open Setup on Mission Control and tap the button.")))
import doer  # noqa: E402
out = doer.pc_only("The export never appeared. Open the file on the PC and check it.")
check("the doer's result line applies it too, and its fallback no longer says 'needs the PC'", not chatfirst.violations(out) and "needs the PC" not in out, out)
import cards  # noqa: E402
import chat_push  # noqa: E402
q = cards.connection_card("mail-x", "Mail", "the login was refused", "python assistant/scripts/setup_creds.py mail x", "A console opens on the PC asking for the app password.")
check("a connection card never carries the registry's renew line or a console step", "Renew" not in q["details"] and not clean_of(q["details"]) and not clean_of(q["question"]) and not clean_of(q["advice"]), q["details"])
body = chat_push.card_body({"id": "q0123456789ab", "status": "open", "questions": [{
    "question": "The feed stopped. Open the Setup page and tap Bank feed.", "header": "Bank feed down", "lane": "personal",
    "details": "Run python assistant/x.py in a terminal on the PC.", "advice": "Sign in on the PC first.", "options": [{"label": "Set it up now"}, {"label": "Leave it"}]}]})
txt = json.dumps([s for s in body["cardsV2"][0]["card"]["sections"]], ensure_ascii=False)
check("the card renderer is the net under every card: ask, details and advice come out chat-first", not clean_of(re.sub(r"Also on <a[^>]*>Mission Control</a>", "", txt)), txt[:300])

# ------------------------------------------------------------------ 3. the sweep: what the engine itself says
import tickets  # noqa: E402
import report  # noqa: E402
tickets.add_many([{"source": "t", "source_id": "cf", "lane": "personal", "kind": "agent_run", "title": "Renew the permit", "summary": "x",
                   "payload": {"text": "Renew the permit", "from": "owner"}}], "test")
t = tickets.claim(limit=1, by="doer", kind="agent_run")[0]
TID = t["id"]
said = {
    "login card (no vault login)": report.build_login_question(TID, "city.example", "https://city.example/login", "NO_ENTRY host=city.example")["question"],
    "login card (captcha)": report.build_login_question(TID, "city.example", "https://city.example/login", "FAILED captcha on the login page; a person has to pass it")["question"],
    "code checkpoint": report.build_question(TID, {"step": "Type the code", "values": {}, "resume": "carry on", "ask_code": True})["question"],
    "hand-off checkpoint (browser in front)": report.build_question(TID, {"step": "Pay at checkout", "values": {}, "resume": "x", "handoff": True}, (True, "ok"))["question"],
    "hand-off checkpoint (not confirmed)": report.build_question(TID, {"step": "Pay at checkout", "values": {}, "resume": "x", "handoff": True}, (False, "minimized"))["question"],
    "blocked line": doer._say("owner", "DOER_BLOCKED", "the site wants a verification code || DO IT YOURSELF: https://x.example then tap Verify", t),
    "no-result line": doer._say("owner", "", "it ran out of steps", t),
    "budget fallback (PC_LINE)": doer.PC_LINE,
}
doer._login_card = lambda qid: {"host": "city.example", "human": False}
said["login checkpoint line"] = doer._say("owner", "DOER_CHECKPOINT", "q0123456789ab sign in to city.example", t)
doer._login_card = lambda qid: {"host": "city.example", "human": True}
said["captcha checkpoint line"] = doer._say("owner", "DOER_CHECKPOINT", "q0123456789ab sign in to city.example", t)
doer._login_card = lambda qid: {}
doer._handoff = lambda t, since=0.0: {"qid": "q0123456789ab", "on_screen": True}
said["hand-off line"] = doer._say("owner", "DOER_CHECKPOINT", "q0123456789ab Pay at checkout", t)
bad = {k: clean_of(v) for k, v in said.items() if clean_of(v)}
check("the doer's cards and lines: sign-in, captcha, code, hand-off, blocked, no result", not bad, repr(bad) + " | " + repr({k: said[k][:160] for k in bad}))

import setup_actions  # noqa: E402
import chat_forms  # noqa: E402
import home_tab  # noqa: E402
import startup  # noqa: E402
import livecard  # noqa: E402
home_tab.today_events = lambda: []
home_tab.pc_health = lambda: {"cpu_pct": 5}
startup.pending = lambda fresh=True: []
views = {name: fn("home") for name, fn in home_tab.VIEWS.items()}
bad = {k: clean_of(v) for k, v in views.items() if clean_of(v)}
check("every Home tab view", not bad, repr(bad))
live = livecard.body(t, {"state": "running", "started": 1, "on_screen": True, "shot_at": 1})
check("the live job card, with its browser brought to the front", not clean_of(live), repr(clean_of(live)) + json.dumps(live, ensure_ascii=False)[:300])
jobs = {"job card": chat_forms.job_card("chat-home"), "job needs login": chat_forms.job_card("chat-home", result={"ok": False, "state": "needs_login", "site": "accounts.example.com", "message": "accounts.example.com wants a sign-in and my vault has no login for it. Tap Enter login."}),
        "start-up card": startup.card(st=[{"rid": "a", "name": "A", "how": "I do it myself.", "done": False}]), "vault dialog": chat_push.vault_dialog("x.example", rid="chat-home"),
        "code dialog": chat_push.code_dialog({"id": "q0123456789ab", "questions": [{"header": "Code", "exec": {"payload": {"host": "x.example"}}}]}),
        "vault card": __import__("vault_chat").card("x.example", "the agent")}
bad = {k: clean_of(v) for k, v in jobs.items() if clean_of(v)}
check("the setup job card, the start-up card, the vault and code dialogs", not bad, repr(bad))

# the setup runner from chat: every kind of row answers with a chat-first line (nothing is opened on the PC)
opened = []
setup_actions._console = lambda argv, title: opened.append(("console", title)) or {"ok": True, "message": "A console window opened on the PC."}
setup_actions._browser = lambda op, url="", timeout=90: opened.append(("browser", op)) or {"ok": True, "message": "The agent's browser is on screen."}
setup_actions._signin_from_vault = lambda name, url: {"ok": False, "state": "needs_login", "site": "x.example", "message": f"{name} is signed out and my vault has no login for x.example. Tap Enter login."}
setup_actions._gap = lambda rid, name, why, by: None
results = {}
for row in setup_actions._registry():
    rid = str(row.get("id"))
    kind = str(setup_actions.recipe(row).get("kind"))
    if kind == "job":
        continue                                         # runs a real job; covered in test_home_tab / test_startup
    try:
        results[f"{rid} ({kind})"] = setup_actions.run_chat(rid, by="card")
    except Exception as e:  # noqa: BLE001
        results[f"{rid} ({kind})"] = {"message": f"CRASH {type(e).__name__}: {e}"}
bad = {k: (clean_of(v.get("message", "")), v.get("message", "")[:120]) for k, v in results.items() if clean_of(v.get("message", "")) or "CRASH" in str(v.get("message"))}
check("setup from chat: every connection row answers chat-first, and nothing is opened on the PC", not bad and not opened, repr(bad) + repr(opened[:3]))
check("setup from chat covers the registry", len(results) >= 5, str(len(results)))

# the registry words that reach a card (setup.how through the card's net) and the config lines said in chat
reg_bad = {}
for row in setup_actions._registry():
    how = str((row.get("setup") or {}).get("how") or "")
    shown = cards._chat_first(f"How I set it back up: {how}") if how else ""
    if clean_of(shown):
        reg_bad[row["id"]] = clean_of(shown)
check("no connection's setup words reach a card with a PC step in them", not reg_bad, repr(reg_bad))
cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
say = str(((cfg.get("doer") or {}).get("signin_window") or {}).get("say") or "")
check("the signed-out line in config is chat-first", not clean_of(say), say)

# the prompts: they state the rule, and none tells the model to point at the Setup page or the PC
PROMPTS = ["agent/prompt.md", "agent/templates/prompt.generic.md", "doer/prompt.md", "doer/computer_prompt.md"]
pointing = re.compile(r"(?<!Never )name the Setup page|on the Setup page with a Sign in button|sign(?:s)? in by hand|open it with browser\.py --show", re.I)
bad = {f: pointing.findall((ROOT / f).read_text(encoding="utf-8")) for f in PROMPTS if pointing.search((ROOT / f).read_text(encoding="utf-8"))}
check("no prompt tells the model to send an owner to the Setup page or the PC", not bad, repr(bad))
check("the agent's prompt states the rule (inside this chat, a portal into the PC)",
      all("portal into the PC" in (ROOT / f).read_text(encoding="utf-8") for f in PROMPTS[:2]))

print()
print("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}", f" ({TMP})")
sys.exit(1 if FAILS else 0)
