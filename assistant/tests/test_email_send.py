"""Offline tests for email_send.py's outcome reporting and how drain.py reads it.

No network: smtplib.SMTP is replaced by a fake. The point is the three outcomes -
NOT_SENT (retry is safe), SENT=1, DELIVERY_UNVERIFIED (never retried blind).
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))   # never the live ~/.claude logs
import contextlib
import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="wv_mail_"))
os.environ["ASSISTANT_DATA"] = str(TMP)
sys.path.insert(0, str(HERE.parent / "scripts"))
import email_send  # noqa: E402
import drain  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


class FakeSMTP:
    mode = "ok"
    partial = {}
    sent = 0

    def __init__(self, *a, **k):
        if FakeSMTP.mode == "connect":
            raise OSError("network unreachable")

    def ehlo(self): pass
    def starttls(self): pass

    def login(self, *a):
        if FakeSMTP.mode == "auth":
            raise email_send.smtplib.SMTPAuthenticationError(535, b"bad credentials")

    def send_message(self, *a, **k):
        if FakeSMTP.mode == "mid":
            raise TimeoutError("timed out during DATA")
        if FakeSMTP.mode == "refused":
            raise email_send.smtplib.SMTPRecipientsRefused({"x@example.com": (550, b"no such user")})
        FakeSMTP.sent += 1
        FakeSMTP.last = a[0] if a else None
        return dict(FakeSMTP.partial)

    def quit(self):
        if FakeSMTP.mode == "quit":
            raise TimeoutError("timed out on QUIT")

    def close(self): pass


email_send.smtplib.SMTP = FakeSMTP
email_send.secret = lambda name: "app-password"
body = TMP / "body.txt"
body.write_text("Hi Sara,\n\nSee you Saturday.\n\nowner1", encoding="utf-8")


def run(mode):
    FakeSMTP.mode, FakeSMTP.sent = mode, 0
    sys.argv = ["email_send.py", "--from", "owner3@example.invalid", "--to", "sara@example.com",
                "--subject", "Saturday", "--body-file", str(body)]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = email_send.main()
    return rc, buf.getvalue().strip()


rc, out = run("ok")
check("clean send reports SENT=1", rc == 0 and out.startswith("SENT=1") and FakeSMTP.sent == 1, out)
FakeSMTP.partial = {"bad@example.com": (550, b"no such user")}
rc, out = run("ok")
check("a partly refused send is SENT with the refused address named", rc == 0 and out.startswith("SENT=1") and "PARTIAL_REFUSED=bad@example.com" in out, out)
FakeSMTP.partial = {}
rc, out = run("quit")
check("a failure closing the connection after acceptance is still SENT", rc == 0 and out.startswith("SENT=1") and FakeSMTP.sent == 1, out)
rc, out = run("connect")
check("connect failure is NOT_SENT", rc == 1 and out.startswith("NOT_SENT at connect"), out)
rc, out = run("auth")
check("auth failure is LOGIN_REQUIRED", rc == 1 and out.startswith("LOGIN_REQUIRED"), out)
rc, out = run("refused")
check("server refusal is NOT_SENT", rc == 1 and out.startswith("NOT_SENT server refused"), out)
rc, out = run("mid")
check("a failure while handing the message over is DELIVERY_UNVERIFIED", rc == 1 and out.startswith("DELIVERY_UNVERIFIED"), out)
sys.argv = ["email_send.py", "--from", "example@other-business.invalid", "--to", "sara@example.com", "--subject", "x", "--body-file", str(body)]
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = email_send.main()
check("property identity is still refused", rc == 1 and buf.getvalue().strip() == "REFUSED=forbidden_identity", buf.getvalue())

# replies: References chain + "Re: <parent subject>", then the sent copy's Gmail thread is checked
PARENT = "<pb96da4@example.invalid>"
GM = {PARENT: {"thrid": "111", "refs": "<root@x.com>", "subject": "Re: Sloane & Bram (6/20) - drone footage?"}}
email_send._gm_lookup = lambda acct, frm, mid, tries=1, wait=3.0: GM.get(mid, {})


def reply(irt, subject="Thank you!", refs=""):
    FakeSMTP.mode, FakeSMTP.sent = "ok", 0
    sys.argv = ["email_send.py", "--from", "owner3@example.invalid", "--to", "sharon@example.com",
                "--subject", subject, "--body-file", str(body), "--in-reply-to", irt] + (["--references", refs] if refs else [])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = email_send.main()
    return rc, buf.getvalue().strip(), FakeSMTP.last


real_mid = email_send.uuid.uuid4
email_send.uuid.uuid4 = lambda: type("U", (), {"hex": "sentid"})()
GM["<sentid@example.invalid>"] = {"thrid": "111"}
rc, out, m = reply("pb96da4@example.invalid")
check("reply: In-Reply-To gets angle brackets", m["In-Reply-To"] == PARENT, m["In-Reply-To"])
check("reply: References = parent's References + its Message-ID", m["References"] == "<root@x.com> " + PARENT, m["References"])
check("reply: subject is exactly 'Re: <original subject>'", m["Subject"] == "Re: Sloane & Bram (6/20) - drone footage?", m["Subject"])
check("reply: same Gmail thread reports THREAD=ok", rc == 0 and out.startswith("SENT=1") and out.endswith("THREAD=ok"), out)
GM["<sentid@example.invalid>"] = {"thrid": "999"}
rc, out, m = reply(PARENT)
check("reply: a different thread is reported as THREAD_MISMATCH (still SENT, never retried)",
      rc == 0 and out.startswith("SENT=1") and "THREAD_MISMATCH=sent:999,parent:111" in out, out)
del GM["<sentid@example.invalid>"]
rc, out, m = reply(PARENT)
check("reply: sent copy not found is THREAD_UNVERIFIED", "THREAD_UNVERIFIED=sent_copy_not_found" in out, out)
rc, out, m = reply("<unknown@x.com>", subject="Drone footage", refs="<a@x.com>")
check("reply: parent not in mailbox keeps given References, adds Re:, THREAD_UNVERIFIED",
      m["References"] == "<a@x.com> <unknown@x.com>" and m["Subject"] == "Re: Drone footage"
      and "THREAD_UNVERIFIED=parent_not_in_mailbox" in out, out)
rc, out = run("ok")
check("a plain (non-reply) send has no thread note", "THREAD" not in out and FakeSMTP.last["References"] is None, out)
email_send.uuid.uuid4 = real_mid

# how drain reads those outcomes
P = {"from": "owner3@example.invalid", "to": "sara@example.com", "subject": "Saturday", "body": "Hi"}


def fake_run(stdout, rc=1, boom=False):
    def _run(cmd, timeout=180):
        if boom:
            raise subprocess.TimeoutExpired(cmd, timeout)
        return subprocess.CompletedProcess(cmd, rc, stdout, "")
    return _run


drain._run = fake_run("SENT=1 <id@example.invalid>", 0)
ok, res = drain._exec_email_send(dict(P), False)
check("drain: SENT is done", ok)
drain._run = fake_run("SENT=1 <id@example.invalid> PARTIAL_REFUSED=bad@example.com", 0)
ok, res = drain._exec_email_send(dict(P), False)
check("drain: a partly refused send is done, not a failure to re-card (double send)", ok and "PARTIAL_REFUSED" in res, res)
drain._run = fake_run("SENT=1 <id@example.invalid> THREAD_MISMATCH=sent:9,parent:1", 0)
ok, res = drain._exec_email_send(dict(P), False)
line = drain._result_line({"kind": "email_send", "payload": P}, res)
check("drain: a thread mismatch is sent (no re-send) but the result line says it is not in the thread",
      ok and "NEW thread" in line, line)
line = drain._result_line({"kind": "email_send", "payload": P}, "SENT=1 <id@example.invalid> THREAD=ok")
check("drain: a verified reply says so", "reply in the original thread" in line, line)
drain._run = fake_run("REFUSED=bad_recipient", 1)
ok, res = drain._exec_email_send(dict(P), False)
check("drain: a whole refusal is still permanent", not ok and res.startswith(drain.PERM), res)
drain._run = fake_run("NOT_SENT at connect: OSError: down")
ok, res = drain._exec_email_send(dict(P), False)
check("drain: NOT_SENT is transient (retryable)", not ok and not res.startswith(drain.PERM), res)
drain._run = fake_run("DELIVERY_UNVERIFIED TimeoutError: timed out")
ok, res = drain._exec_email_send(dict(P), False)
check("drain: DELIVERY_UNVERIFIED is permanent", not ok and res.startswith(drain.UNVERIFIED), res)
drain._run = fake_run("Traceback (most recent call last): ...")
ok, res = drain._exec_email_send(dict(P), False)
check("drain: an unrecognised failure is never retried", not ok and res.startswith(drain.UNVERIFIED), res)
drain._run = fake_run("", boom=True)
ok, res = drain._exec_email_send(dict(P), False)
check("drain: a timeout is never retried", not ok and res.startswith(drain.UNVERIFIED), res)
drain._run = fake_run("REFUSED=forbidden_identity")
ok, res = drain._exec_email_send(dict(P), False)
check("drain: REFUSED is permanent", not ok and res.startswith(drain.PERM), res)

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)} (scratch {TMP})")
sys.exit(1 if FAILS else 0)
