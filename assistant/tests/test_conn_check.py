"""Offline tests for conn_check (connection registry health) and its sentinel finding."""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))   # never the live ~/.claude logs
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="wv_cc_"))
os.environ["ASSISTANT_DATA"] = str(TMP)
sys.path.insert(0, str(HERE.parent / "scripts"))
import conn_check as cc  # noqa: E402
import sentinels  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def iso(hours_ago):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def write(name, obj):
    (TMP / name).write_text(json.dumps(obj), encoding="utf-8")


# ---- status_file
write("s_ok.json", {"last_ok": iso(0.2), "error": ""})
write("s_old.json", {"last_ok": iso(9), "error": ""})
write("s_err.json", {"last_ok": iso(0.2), "error": "HTTP 401 token rejected"})
sf = lambda p, **kw: cc.check_status_file({"path": f"data:{p}", "ts_field": "last_ok", "max_age_h": 3, "error_field": "error", **kw})  # noqa: E731
check("status file fresh -> live", sf("s_ok.json")[0] == cc.LIVE, sf("s_ok.json"))
check("status file stale -> dead", sf("s_old.json")[0] == cc.DEAD, sf("s_old.json"))
check("status file with error -> dead, error shown", sf("s_err.json") == (cc.DEAD, "HTTP 401 token rejected"), sf("s_err.json"))
check("status file missing -> not_configured", sf("nope.json")[0] == cc.NOT_CONFIGURED)
write("s_side.json", {"error": "connector grant expired"})
check("error read from a side file", sf("s_ok.json", error_path="data:s_side.json") == (cc.DEAD, "connector grant expired"))

# ---- env
os.environ["WV_CC_TEST_SET"] = "x"
check("env set -> live", cc.check_env({"name": "WV_CC_TEST_SET"})[0] == cc.LIVE)
check("env unset -> not_configured", cc.check_env({"name": "WV_CC_TEST_NEVER_SET_1234"})[0] == cc.NOT_CONFIGURED)

# ---- cmd / cmd_output
py = Path(sys.executable).name
os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
check("cmd exit 0 -> live", cc.check_cmd({"argv": [py, "-c", "pass"]})[0] == cc.LIVE)
check("cmd exit 3 -> dead", cc.check_cmd({"argv": [py, "-c", "import sys; sys.exit(3)"]})[0] == cc.DEAD)
check("cmd not installed -> not_configured", cc.check_cmd({"argv": ["wv-no-such-cli-xyz"]})[0] == cc.NOT_CONFIGURED)
check("cmd_output empty -> not_configured", cc.check_cmd_output({"argv": [py, "-c", "pass"]})[0] == cc.NOT_CONFIGURED)
r = cc.check_cmd_output({"argv": [py, "-c", "print('origin')"]})
check("cmd_output with output -> live", r == (cc.LIVE, "origin"), r)

# ---- cmd_match: the OUTPUT says the state (vault.py has -> ENTRY / NO_ENTRY), exit code alone does not
cm = lambda prog, **kw: cc.check_cmd_match({"argv": [py, "-c", prog], "live": "^ENTRY ", "missing": "^NO_ENTRY ", **kw})  # noqa: E731
r = cm("print('ENTRY NEXT Insurance user=j*** totp=no')")
check("cmd_match live regex -> live, first line as detail", r == (cc.LIVE, "ENTRY NEXT Insurance user=j*** totp=no"), r)
r = cm("import sys; print('NO_ENTRY login.nextinsurance.com'); sys.exit(1)", missing_detail="no login in the vault")
check("cmd_match missing regex -> not_configured with the plain detail (exit 1 does not make it dead)", r == (cc.NOT_CONFIGURED, "no login in the vault"), r)
r = cm("print('vault locked: bad key')")
check("cmd_match neither -> dead, output shown", r[0] == cc.DEAD and "vault locked" in r[1], r)
check("cmd_match not installed -> not_configured", cc.check_cmd_match({"argv": ["wv-no-such-cli-xyz"], "live": "x"})[0] == cc.NOT_CONFIGURED)
reg_rows = json.loads((HERE.parent / "connections.json").read_text(encoding="utf-8"))["connections"]
nx = next((r for r in reg_rows if r["id"] == "next-insurance"), None)
check("the registry has the NEXT Insurance row as a cmd_match on the doer vault, with a Sign in button",
      nx and nx["check"]["type"] == "cmd_match" and "vault.py" in " ".join(nx["check"]["argv"]) and nx["setup"]["kind"] == "signin" and nx["lane"] == "money", nx)

# ---- json_fields
write("cfg.json", {"google": {"customer_id": "TODO"}, "meta": {"page_id": "123", "empty": None}})
jf = lambda *f: cc.check_json_fields({"path": "data:cfg.json", "fields": list(f)})  # noqa: E731
check("TODO field -> not_configured", jf("google.customer_id")[0] == cc.NOT_CONFIGURED)
check("None field -> not_configured", jf("meta.empty")[0] == cc.NOT_CONFIGURED)
check("filled field -> live", jf("meta.page_id")[0] == cc.LIVE)

# ---- http against a port nothing listens on
check("http refused -> dead", cc.check_http({"url": "http://127.0.0.1:9/"})[0] == cc.DEAD)

# ---- mcp: an unregistered server with an empty credential folder
(TMP / "cred").mkdir()
r = cc.check_mcp({"server": "wv-test-no-such-server", "cred_dir": str(TMP / "cred")})
check("mcp unregistered -> not_configured", r[0] == cc.NOT_CONFIGURED, r)

# ---- run(): registry -> status file, state bookkeeping, one crashing row does not hide others
reg = TMP / "registry.json"
reg.write_text(json.dumps({"connections": [
    {"id": "a", "name": "Sensor A", "lane": "wedding", "owner": "owner1", "renew": "set the token", "check": {"type": "status_file", "path": "data:s_ok.json", "ts_field": "last_ok", "max_age_h": 3}},
    {"id": "b", "name": "Thing B", "check": {"type": "manual"}},
    {"id": "c", "name": "Broken row", "check": {"type": "status_file"}},
    {"id": "d", "name": "Unknown type", "check": {"type": "no-such-type"}},
]}), encoding="utf-8")
out = cc.run(registry=reg)
check("run: live row", out["a"]["state"] == cc.LIVE and out["a"]["last_live"])
check("run: manual row", out["b"]["state"] == cc.MANUAL)
check("run: crashing row is dead, others still checked", out["c"]["state"] == cc.DEAD and out["d"]["state"] == cc.DEAD and len(out) == 4, out["c"])
check("run: status file written", set(json.loads((TMP / "connections_status.json").read_text(encoding="utf-8"))["connections"]) == {"a", "b", "c", "d"})
since = out["a"]["since"]
write("s_ok.json", {"last_ok": iso(9), "error": ""})        # the sensor goes quiet
out = cc.run(registry=reg)
check("run: one failed check after live is a strike, still live (2026-10-25)", out["a"]["state"] == cc.LIVE and out["a"].get("strike") and "one failed check" in out["a"]["detail"], out["a"])
out = cc.run(registry=reg)
check("run: flip to dead keeps last_live", out["a"]["state"] == cc.DEAD and out["a"]["last_live"])
out2 = cc.run(registry=reg)
check("run: 'since' holds while the state holds", out2["a"]["since"] == out["a"]["since"])
only = cc.run(registry=reg, only="b")
check("run --only keeps the other rows", set(only) == {"a", "b", "c", "d"})
check("summary line", cc.summary(out).startswith("CONN live=0 dead=3 not_configured=0 manual=1"), cc.summary(out))

# ---- a sensor error that is not about sign-in is a code fault, never "re-authorise it" (HoneyBook 10/9-10/13)
reg2 = TMP / "registry2.json"
row = {"id": "hb", "name": "HB", "owner": "owner1", "renew": "re-authorise", "auth_errors": r"unauthori[sz]ed|\b401\b|oauth",
       "fault_renew": "fix the code", "check": {"type": "status_file", "path": "data:s_hb.json", "ts_field": "last_ok", "max_age_h": 3}}
reg2.write_text(json.dumps({"connections": [row]}), encoding="utf-8")
write("s_hb.json", {"last_ok": iso(1), "error": "module does not provide an export named 'ReportingAdapter'"})
hb = cc.run(registry=reg2)["hb"]
check("fault: code error -> claude fixes, no re-auth ask", hb["owner"] == "claude" and hb["renew"] == "fix the code" and hb["detail"].startswith("code fault"), hb)
write("s_hb.json", {"last_ok": iso(1), "error": "HTTP 401 Unauthorized"})
hb = cc.run(registry=reg2)["hb"]
check("fault: auth error still asks the owner to re-authorise", hb["owner"] == "owner1" and hb["renew"] == "re-authorise", hb)
write("s_hb.json", {"last_ok": iso(9), "error": ""})
hb = cc.run(registry=reg2)["hb"]
check("fault: plain staleness keeps the normal renew", hb["renew"] == "re-authorise", hb)

# ---- no check result may leak a secret-looking value: details are short, fixed phrases
check("details are bounded", all(len(c["detail"]) <= 160 for c in out.values()))

# ---- sentinel: dead -> finding with the fix; not_configured / manual / live -> none
write("connections_status.json", {"ts": iso(0), "connections": {
    "x": {"name": "Mail X", "state": "dead", "detail": "IMAP login refused", "renew": "run setup_creds", "owner": "owner1"},
    "y": {"name": "Cal Y", "state": "not_configured", "detail": "not stored"},
    "z": {"name": "Z", "state": "live", "detail": "ok"},
    "m": {"name": "M", "state": "manual", "detail": ""}}})
f = [x for x in sentinels.collect() if x["key"].startswith("conn:")]
check("sentinel: only the dead connection is a finding", [x["key"] for x in f] == ["conn:x"], f)
check("sentinel: finding names the fix", f and "Mail X" in f[0]["text"] and "run setup_creds" in f[0]["text"], f)

# ---- a chat-setup connection's alarm card: chat words, no command line, no item id (Plaid card, 2026-10-17)
import cards  # noqa: E402
_posted = []
cards.post = lambda qs, *a, **kw: (_posted.extend(qs), {"posted": True})[1]
sentinels._conn_card({"id": "bank-plaid", "name": "Bank feed: Relay via Plaid", "renew": "python assistant/scripts/money/plaid.py setup",
                      "detail": "aBc123XyZ987qwe456RtY0mNb: ITEM_LOGIN_REQUIRED - sign in again: python assistant/scripts/money/plaid.py setup --update aBc123XyZ987qwe456RtY0mNb"})
pc = json.dumps(_posted)
check("plaid card: tells them to say 'set up plaid' in chat", "set up plaid" in pc, pc)
check("plaid card: no command line, no item id", "python" not in pc and "plaid.py" not in pc and "aBc123" not in pc and "terminal" not in pc, pc)
check("plaid card: still says what broke", "ITEM_LOGIN_REQUIRED" in pc, pc)
# ---- the SimpleFIN card points at the Setup page's Bank feed button, never the terminal (2026-10-15)
_posted.clear()
sentinels._conn_card({"id": "bank-simplefin", "name": "Bank feed (SimpleFIN Bridge, read-only)",
                      "renew": "python assistant/scripts/setup_creds.py money simplefin", "detail": "HTTP 403 - python assistant/scripts/setup_creds.py money simplefin"})
sc = json.dumps(_posted)
check("simplefin card: points to the Bank feed button on Setup", "/setup" in sc and "Bank feed button" in sc, sc)
check("simplefin card: no command, no terminal", "python" not in sc and "setup_creds" not in sc and "terminal" not in sc.lower(), sc)
check("simplefin card: still says what broke", "HTTP 403" in sc, sc)

# ---- once a Plaid feed is set up, a dead SimpleFIN feed is no finding (no card); before that it is
def _bank(plaid_state):
    conns = {"bank-simplefin": {"name": "Bank feed (SimpleFIN)", "state": "dead", "detail": "stale", "renew": "x"}}
    if plaid_state:
        conns["bank-plaid"] = {"name": "Bank feed: Relay via Plaid", "state": plaid_state, "detail": "ok"}
    write("connections_status.json", {"ts": iso(0), "connections": conns})
    return [x["key"] for x in sentinels.collect() if x["key"].startswith("conn:")]
check("simplefin: carded while Plaid is not set up", _bank(None) == ["conn:bank-simplefin"] and _bank("not_configured") == ["conn:bank-simplefin"])
check("simplefin: no card once Plaid is set up", _bank("live") == [] and _bank("dead") == ["conn:bank-plaid"])
check("detail with nothing left falls back to plain words", sentinels._plain("python x.py setup") == "it stopped answering")

print("\n%d failed" % len(FAILS) if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
