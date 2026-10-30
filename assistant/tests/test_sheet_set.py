"""Offline tests for the card-gated Google Sheets write path (2026-10-18): google_api.sheet_get /
sheet_set against a stand-in Sheets API, the `sheet_set` exec kind (exec_store), the card guard (cards:
every cell + value verbatim, Sheets scope present), drain's before-check + RAW write + read-back, and
sheet_fix's plan / card / state machine. Nothing here talks to Google; scratch dirs throughout.
    python assistant/tests/test_sheet_set.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))   # never the live ~/.claude logs
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote

HERE = Path(__file__).resolve().parent
SCRIPTS, AGENT = HERE.parent / "scripts", HERE.parent / "agent"
TMP = Path(tempfile.mkdtemp(prefix="wv_sheet_"))
os.environ["ASSISTANT_DATA"] = str(TMP)
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["RULINGS_FILE"] = str(HERE.parent / "data" / "rulings.json")
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(AGENT))
import google_api as g  # noqa: E402
import cards  # noqa: E402
import exec_store as es  # noqa: E402
import drain  # noqa: E402
import sheet_fix  # noqa: E402
if not g.CONFIG.is_file():                               # the bare engine ships no google.json: the accounts this test names
    g.CONFIG = TMP / "google.json"
    g.CONFIG.write_text(json.dumps({"accounts": {
        "owner1-personal": {"address": "owner4@example.invalid", "person": "owner1", "calendars": ["primary"]},
        "owner1-films": {"address": "owner3@example.invalid", "person": "owner1", "calendars": ["primary"],
                        "extra_scopes": ["https://www.googleapis.com/auth/spreadsheets"]},
        "owner6": {"address": "owner6@example.invalid", "person": "owner6", "calendars": ["primary"]}}}), encoding="utf-8")

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------- stand-ins: token file + Sheets API
g.HOME = TMP / "google"
g.HOME.mkdir()
ACCT, SID = "owner1-films", "xa6cc6e366e128a4d8f4e7b3a2a98aca1ff806d00"


def token(with_sheets=True):
    sc = list(g.SCOPES) + ([g.SHEETS_SCOPE] if with_sheets else [])
    (g.HOME / f"{ACCT}.json").write_text(json.dumps({"refresh_token": "r", "client_id": "c", "client_secret": "s",
                                                      "token": "t", "scopes": sc}), encoding="utf-8")


def fresh_sheet():
    months = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
    mult = [0.95, 0.95, 1.1, 1.1, 1.2, 1.2, 0.95, 0.95, 1.1, 1.2, 1.2, 1.1]
    days = [("Sunday", 1.05), ("Monday", 0.95), ("Tuesday", 0.95), ("Wednesday", 0.95), ("Thursday", 0.95), ("Friday", 1.05), ("Saturday", 1.1)]
    addons = [("first_look", "First Look", 100), ("feature_film", "Feature Film (8-10mins)", 1000), ("raw_footage", "Raw Footage", 1800),
              ("iphone_content_creator", "iPhone Content Creator", 600), ("drone", "Aerial Cinematography/Drone", 500),
              ("second_videographer", "Second Cinematographer", 600), ("vows_reaction_angles", "Vows Reaction Angles", 300),
              ("social_media_content", "Social Media Content", 0), ("signature_couples_shoot", "Signature Couples Shoot", 0)]
    return {"season_multipliers": [["month_number", "month_name", "season", "multiplier"]] + [[i + 1, months[i], "", mult[i]] for i in range(12)],
            "day_multipliers": [["day_index", "day_name", "multiplier"]] + [[i, d, m] for i, (d, m) in enumerate(days)],
            "addons": [["addon_id", "addon_name", "price", "is_active", "category"]] + [[a, n, p, True, "extras"] for a, n, p in addons]}


SHEET = fresh_sheet()
CALLS = []


def _ci(letters):
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def _parse(rng):
    m = re.match(r"^(?:'([^']+)'|([^'!]+))!([A-Z]+)(\d+)(?::([A-Z]+)(\d+))?$", rng)
    assert m, rng
    tab = m.group(1) or m.group(2)
    c1, r1 = _ci(m.group(3)), int(m.group(4)) - 1
    c2, r2 = (_ci(m.group(5)), int(m.group(6)) - 1) if m.group(5) else (c1, r1)
    return tab, r1, c1, r2, c2


def fake_api(name, url, body=None, method=""):
    """A Sheets API that stores cells in SHEET: GET values/{range}, PUT values/{range}?valueInputOption=RAW."""
    CALLS.append((name, url, body, method))
    m = re.search(r"/v4/spreadsheets/([^/]+)/values/([^?]+)\?(.*)$", url)
    assert m and unquote(m.group(1)) == SID, url
    rng, q = unquote(m.group(2)), parse_qs(m.group(3))
    tab, r1, c1, r2, c2 = _parse(rng)
    grid = SHEET.setdefault(tab, [])
    if method == "PUT":
        assert q.get("valueInputOption") == ["RAW"], q
        n = 0
        for i, row in enumerate(body["values"]):
            while len(grid) <= r1 + i:
                grid.append([])
            for j, v in enumerate(row):
                while len(grid[r1 + i]) <= c1 + j:
                    grid[r1 + i].append("")
                grid[r1 + i][c1 + j] = v
                n += 1
        return {"updatedRange": rng, "updatedCells": n}
    assert method in ("", "GET")
    out = []
    for i in range(r1, min(r2, len(grid) - 1) + 1):
        row = [grid[i][j] if j < len(grid[i]) else "" for j in range(c1, c2 + 1)]
        while row and g.cell_text(row[-1]) == "":
            row.pop()
        out.append(row)
    while out and not out[-1]:
        out.pop()
    return {"values": out} if out else {}


g._api = fake_api
token(with_sheets=True)

# ---------------------------------------------------------------- 1. google_api helpers + Sheets calls
check("a1_split / a1_join", g.a1_split("season_multipliers!D2") == ("season_multipliers", "D2") and g.a1_split("'My Tab'!AB10") == ("My Tab", "AB10")
      and g.a1_join("My Tab", "D2") == "'My Tab'!D2" and g.a1_join("addons", "A12") == "addons!A12")
for bad in ("season!D2:D3", "D", "!D2", "season!d2", ""):
    try:
        g.a1_split(bad); check(f"a1_split refuses {bad!r}", False)
    except ValueError:
        check(f"a1_split refuses {bad!r}", True)
check("cell_text normalises numbers, booleans, text", [g.cell_text(x) for x in (1, 1.0, 0.95, True, "0.95", " TRUE ", "Full Toasts", None, "", 500)]
      == ["1", "1", "0.95", "TRUE", "0.95", "TRUE", "Full Toasts", "", "", "500"])
check("is_formula", g.is_formula("=SUM(A1)") and g.is_formula("  =1") and not g.is_formula("1") and not g.is_formula(1))
check("scopes_for adds spreadsheets only to the declared account", g.SHEETS_SCOPE in g.scopes_for("owner1-films") and g.SHEETS_SCOPE not in g.scopes_for("owner1-personal"))
check("sheet_ready with the scope", g.sheet_ready(ACCT) == (True, ""))
check("sheet_get reads one cell unformatted", g.sheet_get(ACCT, SID, "season_multipliers!D2") == [[0.95]])
check("sheet_get on an empty cell is []", g.sheet_get(ACCT, SID, "addons!A11") == [])
n0 = len(CALLS)
try:
    g.sheet_set(ACCT, SID, "season_multipliers!D2", [["=1"]]); check("sheet_set refuses a formula", False)
except ValueError as e:
    check("sheet_set refuses a formula before any API call", "formula" in str(e) and len(CALLS) == n0)
try:
    g.sheet_set(ACCT, SID, "season_multipliers!D2", [[None]]); check("sheet_set refuses None", False)
except ValueError:
    check("sheet_set refuses None", True)
rng = g.sheet_set(ACCT, SID, "season_multipliers!D3", [[1]])
check("sheet_set writes RAW by PUT and returns the range", rng == "season_multipliers!D3" and CALLS[-1][3] == "PUT"
      and "valueInputOption=RAW" in CALLS[-1][1] and SHEET["season_multipliers"][2][3] == 1)
SHEET["season_multipliers"][2][3] = 0.95
token(with_sheets=False)
ok, why = g.sheet_ready(ACCT)
check("without the scope: sheet_ready names the one consent command", not ok and "SHEET_SCOPE_MISSING" in why and f"auth {ACCT}" in why, why)
try:
    g.sheet_set(ACCT, SID, "season_multipliers!D2", [[1]]); check("sheet_set refuses without the scope", False)
except PermissionError:
    check("sheet_set refuses without the scope", True)
(g.HOME / f"{ACCT}.json").unlink()
check("not connected: sheet_ready says auth", "not connected" in g.sheet_ready(ACCT)[1])
token(with_sheets=True)
check("mail/calendar scopes untouched, no send scope anywhere", not any(s.endswith(x) for s in g.scopes_for(ACCT) for x in ("gmail.send", "gmail.compose", "gmail.modify", "mail.google.com/"))
      and "https://www.googleapis.com/auth/gmail.readonly" in g.scopes_for(ACCT) and "https://www.googleapis.com/auth/calendar.events" in g.scopes_for(ACCT))

# ---------------------------------------------------------------- 2. exec_store: the sheet_set payload contract
GOOD = {"account": ACCT, "spreadsheet_id": SID, "sheet_name": "Packages Price Input",
        "cells": [{"a1": "season_multipliers!D2", "value": 1, "before": 0.95},
                  {"a1": "season_multipliers!D3", "value": 1, "before": 0.95},
                  {"a1": "addons!A11", "value": "toasts_speeches_film", "before": ""},
                  {"a1": "addons!B11", "value": "Full Toasts & Speeches Film", "before": ""},
                  {"a1": "addons!C11", "value": 500, "before": ""},
                  {"a1": "addons!D11", "value": True, "before": ""}]}
check("good payload validates", es.validate_payload("sheet_set", GOOD) == "", es.validate_payload("sheet_set", GOOD))
check("sheet_set is a lane of its own and NOT outbound", es.KIND_LANES["sheet_set"] == "sheets" and "sheet_set" not in es.OUTBOUND_KINDS)


def mutated(fn):
    p = json.loads(json.dumps(GOOD))
    fn(p)
    return es.validate_payload("sheet_set", p)


check("a formula value is refused", "formula" in mutated(lambda p: p["cells"][0].update(value="=SUM(A1)")))
check("a missing before is refused", "before" in mutated(lambda p: p["cells"][0].pop("before")))
check("a range is refused (single cells only)", "single cell" in mutated(lambda p: p["cells"][0].update(a1="season_multipliers!D2:D9")))
check("an account not declared for Sheets is refused", "not declared" in mutated(lambda p: p.update(account="owner1-personal")))
check("an unknown account is refused", "not in" in mutated(lambda p: p.update(account="example")))
check("a duplicate cell is refused", "twice" in mutated(lambda p: p["cells"].append(dict(p["cells"][0]))))
check("a cell with no tab anywhere is refused", "names no tab" in mutated(lambda p: p["cells"][0].update(a1="D2")))
check("payload.tab supplies the tab for bare cells", es.sheet_cells({"account": ACCT, "spreadsheet_id": SID, "tab": "addons", "cells": [{"a1": "A11", "value": "x", "before": ""}]}) == [("addons!A11", "x", "")])
try:
    es.enqueue("sheet_set", json.loads(json.dumps(GOOD, ).replace('"value": 1,', '"value": "=1",', 1)), "owner1", "card:qx"); check("enqueue refuses a formula", False)
except ValueError as e:
    check("enqueue refuses a formula", "formula" in str(e))

# ---------------------------------------------------------------- 3. cards: every cell and value verbatim; scope present
def q_for(payload, text=None):
    lines = [f"{ref}: {g.cell_text(b) or '(empty)'} -> {g.cell_text(v)}" for ref, v, b in es.sheet_cells(payload)]
    return {"question": text if text is not None else "Pricing sheet: write these cells.\n" + "\n".join(lines), "header": "Pricing sheet", "lane": "wedding",
            "options": [{"label": "Write the cells"}, {"label": "Skip"}],
            "exec": {"kind": "sheet_set", "approve_label": "Write the cells", "payload": payload}}


full = q_for(GOOD)
r = cards.post([dict(full, question=full["question"].replace("addons!C11: (empty) -> 500\n", ""))], "test")
check("card missing one cell is refused (EXEC_VERBATIM)", not r["posted"] and "EXEC_VERBATIM" in r["reason"] and "addons!C11" in r["reason"], r)
r = cards.post([dict(full, question=full["question"].replace("-> 500", "-> 5OO"))], "test")
check("card with a value owner1 cannot read is refused", not r["posted"] and "EXEC_VERBATIM" in r["reason"] and "'500'" in r["reason"], r)
r = cards.post([q_for(json.loads(json.dumps(GOOD).replace('"value": 1,', '"value": "=1",', 1)))], "test")
check("card with a formula value is refused at post time", not r["posted"] and "EXEC_KEYS" in r["reason"] and "formula" in r["reason"], r)
token(with_sheets=False)
r = cards.post([full], "test")
check("card refused while the token lacks the Sheets scope", not r["posted"] and "SHEET_SCOPE_MISSING" in r["reason"], r)
token(with_sheets=True)
r = cards.post([full], "test")
check("complete card posts", r["posted"], r)
qid = r["qid"]
check("pending shows the kind", any(p["qid"] == qid and p["exec_kind"] == "sheet_set" for p in cards.pending()))

# ---------------------------------------------------------------- 4. answer -> ticket -> drain (dry, stale, real)
drain.UNDO_FLOOR_S = 0
a = cards.answer(qid, "Write the cells")
check("approve mints a sheet_set ticket", a["ok"] and a["ticket"] and es.fold()[a["ticket"]]["kind"] == "sheet_set", a)
n0 = len(CALLS)
res = drain.drain(dry=True)
t = es.fold()[a["ticket"]]
check("dry run re-reads every cell, writes nothing, leaves the ticket queued",
      res["done"] == 1 and t["status"] == "queued" and t["result"].startswith("DRY") and SHEET["season_multipliers"][1][3] == 0.95
      and not any(c[3] == "PUT" for c in CALLS[n0:]) and sum(1 for c in CALLS[n0:] if c[3] in ("", "GET")) == len(GOOD["cells"]), (res, t["result"]))
SHEET["season_multipliers"][1][3] = 1.1              # the sheet changed under the card
n0 = len(CALLS)
res = drain.drain()
t = es.fold()[a["ticket"]]
check("stale before -> whole ticket refused, nothing written",
      res["exception"] == 1 and t["status"] == "exception" and "nothing written" in t["result"] and "D2 now holds 1.1" in t["result"]
      and not any(c[3] == "PUT" for c in CALLS[n0:]) and SHEET["season_multipliers"][2][3] == 0.95 and SHEET["addons"][-1][0] == "signature_couples_shoot", t["result"])
check("the exception surfaces as an operator ticket", (TMP / "tickets.json").exists() and "exc|" in (TMP / "tickets.json").read_text(encoding="utf-8"))
SHEET["season_multipliers"][1][3] = 0.95
r = cards.post([full], "test")
a2 = cards.answer(r["qid"], "Write the cells")
n0 = len(CALLS)
res = drain.drain()
t2 = es.fold()[a2["ticket"]]
puts = [c for c in CALLS[n0:] if c[3] == "PUT"]
check("fresh before -> every cell written RAW, read back, ticket done",
      res["done"] == 1 and t2["status"] == "done" and "SHEET_DONE=6" in t2["result"] and "VERIFIED=yes" in t2["result"] and len(puts) == 6
      and all("valueInputOption=RAW" in c[1] for c in puts) and SHEET["season_multipliers"][1][3] == 1 and SHEET["season_multipliers"][2][3] == 1
      and SHEET["addons"][10][:4] == ["toasts_speeches_film", "Full Toasts & Speeches Film", 500, True], (res, t2["result"]))
check("types survive: the number is a number, the flag a boolean, not text", isinstance(SHEET["addons"][10][2], int) and SHEET["addons"][10][3] is True)
line = drain._result_line(t2, t2["result"])
check("result line is plain evidence for the owner", "Packages Price Input" in line and "6 cells written" in line and "read back as written" in line, line)
notes = (TMP / "cards" / "notes.jsonl").read_text(encoding="utf-8") if (TMP / "cards" / "notes.jsonl").exists() else ""
check("result reported under the card", "cells written" in notes and r["qid"] in notes)
es._append(es.TICKETS, {"id": "tk-stalesheet", "ts": "2026-10-18T10:00:00", "kind": "sheet_set", "lane": "sheets", "payload": GOOD,
                        "payload_sha": es.payload_sha(GOOD), "approved_by": "owner1", "approval_ref": "card:qold", "status": "running",
                        "ts_run": "2026-10-18T10:00:00", "attempts": 1})
drain.reap_stale_running()
check("a drain that died mid-write leaves WRITE UNVERIFIED, never a blind retry", es.fold()["tk-stalesheet"]["status"] == "exception"
      and "WRITE UNVERIFIED" in es.fold()["tk-stalesheet"]["result"] and "sheet" in es.fold()["tk-stalesheet"]["result"])

# ---------------------------------------------------------------- 5. sheet_fix: plan from the live tabs, ONE card, state machine
SHEET_EDITS = HERE.parent.parent / "pricing" / "sheet_edits.json"     # the organization's own sheet: absent in the bare engine
if SHEET_EDITS.is_file():
    SHEET.clear()
    SHEET.update(fresh_sheet())
    cfg = sheet_fix.load_config(SHEET_EDITS)
    check("config carries the business facts, not the code", cfg["account"] == ACCT and cfg["spreadsheet_id"] == SID and len(cfg["edits"]) == 3)
    pl = sheet_fix.plan(cfg, g)
    refs = {c["a1"]: c for c in pl["cells"]}
    check("plan finds the month cells by header + key, not by hard-coded letters",
          {"season_multipliers!D2", "season_multipliers!D3", "season_multipliers!D8", "season_multipliers!D9"} <= set(refs)
          and all(refs[f"season_multipliers!D{r}"]["before"] == 0.95 and refs[f"season_multipliers!D{r}"]["value"] == 1 for r in (2, 3, 8, 9)), sorted(refs))
    check("plan finds Mon-Thu by day_index", {f"day_multipliers!C{r}" for r in (3, 4, 5, 6)} <= set(refs) and "day_multipliers!C2" not in refs and "day_multipliers!C7" not in refs)
    check("plan appends the toasts row after the last filled row, copying the extra column from drone",
          refs.get("addons!A11", {}).get("value") == "toasts_speeches_film" and refs.get("addons!B11", {}).get("value") == "Full Toasts & Speeches Film"
          and refs.get("addons!C11", {}).get("value") == 500 and refs.get("addons!D11", {}).get("value") is True and refs.get("addons!E11", {}).get("value") == "extras"
          and all(refs[f"addons!{c}11"]["before"] == "" for c in "ABCDE"), sorted(refs))
    check("13 cells, no problems", len(pl["cells"]) == 13 and not pl["problems"] and not pl["skipped"], (len(pl["cells"]), pl["problems"]))
    qtext = sheet_fix.question(cfg, pl, g)
    check("the question names every cell with before -> after", all(f"{c['a1']}: {g.cell_text(c['before']) or '(empty)'} -> {g.cell_text(c['value'])}" in qtext for c in pl["cells"]))
    check("the question carries the why", "no slow-season or weekday discounts" in qtext)

    sheet_fix.STATE = TMP / "sheet_fix_state.json"
    token(with_sheets=False)
    code, line = sheet_fix.run(cfg, "auto", g)
    check("auto: waits for consent, exit 0, names the command", code == 0 and "waiting for consent" in line and f"auth {ACCT}" in line, line)
    code, line = sheet_fix.run(cfg, "post", g)
    check("by hand: the consent wait exits 3", code == 3)
    token(with_sheets=True)
    code, line = sheet_fix.run(cfg, "plan", g)
    check("plan mode prints the card text and posts nothing", code == 0 and "season_multipliers!D2: 0.95 -> 1" in line and not sheet_fix.open_card(SID))
    code, line = sheet_fix.run(cfg, "auto", g)
    q1 = re.search(r"card (q[0-9a-f]+) posted", line)
    check("auto: posts ONE card", code == 0 and q1 and sheet_fix.open_card(SID) == q1.group(1), line)
    code, line = sheet_fix.run(cfg, "auto", g)
    check("auto: never a second card while one is open", "waiting on card" in line and len([p for p in cards.pending() if p["exec_kind"] == "sheet_set"]) == 1, line)
    cards.answer(q1.group(1), "Skip")
    code, line = sheet_fix.run(cfg, "auto", g)
    check("Skip -> declined, no re-post", "declined" in line and not sheet_fix.open_card(SID), line)
    n0 = len(CALLS)
    code, line = sheet_fix.run(cfg, "auto", g)
    check("declined is remembered without touching the sheet", "declined" in line and len(CALLS) == n0)
    sheet_fix.STATE.unlink()
    code, line = sheet_fix.run(cfg, "auto", g)
    q2 = re.search(r"card (q[0-9a-f]+) posted", line)
    check("a fresh state posts again", bool(q2), line)
    cards.answer(q2.group(1), "Write the cells")
    code, line = sheet_fix.run(cfg, "auto", g)
    check("auto: waits while the ticket is queued", "ticket" in line and "queued" in line, line)
    res = drain.drain()
    check("the spine's drain writes all 13 cells", res["done"] == 1 and SHEET["season_multipliers"][8][3] == 1 and SHEET["day_multipliers"][4][2] == 1
          and SHEET["addons"][10] == ["toasts_speeches_film", "Full Toasts & Speeches Film", 500, True, "extras"], (res, SHEET["addons"][10:]))
    code, line = sheet_fix.run(cfg, "auto", g)
    check("after the write: nothing to do, state done", "nothing to do" in line and json.loads(sheet_fix.STATE.read_text()).get("done"), line)
    n0 = len(CALLS)
    code, line = sheet_fix.run(cfg, "auto", g)
    check("done is remembered: no API call, no card", "done" in line and len(CALLS) == n0 and not sheet_fix.open_card(SID), line)
    pl2 = sheet_fix.plan(cfg, g)
    check("re-plan on the fixed sheet: all skipped, nothing to write", not pl2["cells"] and len(pl2["skipped"]) == 9 and not pl2["problems"], pl2)
    bad = json.loads(json.dumps(cfg))
    bad["edits"][0]["col"] = "multiplyer"
    code, line = sheet_fix.run(bad, "post", g)
    check("a column the sheet does not have is a problem, not a card", code == 2 and "cannot plan" in line and "multiplyer" in line and not sheet_fix.open_card(SID), line)
    bad = json.loads(json.dumps(cfg))
    bad["edits"][0]["keys"] = [13]
    code, line = sheet_fix.run(bad, "post", g)
    check("a key with no row is a problem", code == 2 and "0 rows with month_number=13" in line, line)

    # the CLI against a token dir without the scope never reaches Google (exit 3, the consent line)
    env = dict(os.environ, AGENT_GOOGLE_HOME=str(TMP / "noscope"))
    (TMP / "noscope").mkdir()
    r = subprocess.run([sys.executable, str(SCRIPTS / "sheet_fix.py"), "--plan"], capture_output=True, text=True, env=env, cwd=str(HERE.parent.parent))
    check("CLI without consent: exit 3 and the auth command", r.returncode == 3 and "waiting for consent" in r.stdout and f"auth {ACCT}" in r.stdout, r.stdout + r.stderr)

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)} (scratch {TMP})")
sys.exit(1 if FAILS else 0)
