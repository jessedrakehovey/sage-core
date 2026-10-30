"""email_send.py - the ONE registered email send path for the personal OS.

Only drain.py calls this, with a the owner-approved executor ticket's exact payload. It is
deliberately not on any operator allowlist and the gate baseline denies it to every
LLM cron. Sends through Gmail SMTP (smtp.gmail.com:587, STARTTLS) using the account's
app password from Windows Credential Manager: secret `smtp:<account>` if set, else the
same `imap:<account>` app password (Gmail app passwords work for both).

  python assistant/scripts/email_send.py --from owner3@example.invalid --to p1022b0@example.invalid \
        --subject "..." --body-file body.txt [--cc x@y] [--in-reply-to <msgid>] \
        [--references "<id1> <id2>"] [--dry-run]

Prints SENT=1 <message-id> on success, DRY_RUN_OK on --dry-run, else an error sentinel
and exit 1. A reply (--in-reply-to, or a "Re:" subject found in the mailbox) carries the parent's
References plus its Message-ID and the subject "Re: <parent subject>"; after sending, the sent
message's Gmail thread id is read back and compared to the parent's, adding THREAD=ok,
THREAD_MISMATCH=... or THREAD_UNVERIFIED=... to the SENT=1 line. Identity guard: --from must be one of the accounts in assistant/config.json
and can never be a other-business.invalid address.
"""
from __future__ import annotations

import argparse
import smtplib
import sys
import uuid
from email.message import EmailMessage
from email.utils import formatdate

from alib import ensure_venv
ensure_venv()

from alib import config, secret  # noqa: E402

FORBIDDEN_DOMAINS = ("other-business.invalid",)
_STAGE = {"at": "setup"}   # how far main() got, for the last-resort handler at the bottom


def _account_for(addr: str):
    for a in config().get("mail", {}).get("accounts", []):
        if str(a.get("address", "")).lower() == addr.lower():
            return a
    return None


def _find_thread(acct: dict, frm: str, to: str, subject: str) -> tuple:
    """The newest message with this subject (minus Re:/Fwd:) to or from the recipient, in the
    account's IMAP mailbox -> (Message-ID, References) or ("", ""). Read-only; any failure -> blank."""
    import imaplib
    import re as _re
    from email import message_from_bytes
    from email.header import decode_header
    pw = secret(acct.get("secret", "")) or secret(f"smtp:{acct['name']}")
    host = acct.get("host") or "imap.gmail.com"
    if not pw:
        return "", ""
    core_subj = _re.sub(_RE_PREFIX, "", subject, flags=_re.I).strip()
    if len(core_subj) < 3:
        return "", ""
    try:
        m = imaplib.IMAP4_SSL(host, timeout=25)
        m.login(frm, pw)
        best = ("", "", 0.0)
        for box in ('"[Gmail]/All Mail"', "INBOX"):
            try:
                if m.select(box, readonly=True)[0] != "OK":
                    continue
            except Exception:  # noqa: BLE001
                continue
            for crit in (f'(FROM "{to}")', f'(TO "{to}")'):
                typ, data = m.uid("SEARCH", None, crit, "SUBJECT", f'"{core_subj[:60]}"')
                if typ != "OK" or not data or not data[0]:
                    continue
                for uid in data[0].split()[-15:]:
                    t2, d2 = m.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID REFERENCES DATE SUBJECT)])")
                    if t2 != "OK" or not d2 or not isinstance(d2[0], tuple):
                        continue
                    h = message_from_bytes(d2[0][1])
                    mid = str(h.get("Message-ID") or "").strip()
                    if not mid:
                        continue
                    try:
                        from email.utils import parsedate_to_datetime
                        ts = parsedate_to_datetime(str(h.get("Date"))).timestamp()
                    except Exception:  # noqa: BLE001
                        ts = 0.0
                    if ts >= best[2]:
                        best = (mid, (str(h.get("References") or "") + " " + mid).strip(), ts)
            if best[0]:
                break
        try:
            m.logout()
        except Exception:  # noqa: BLE001
            pass
        return best[0], best[1]
    except Exception:  # noqa: BLE001
        return "", ""


_RE_PREFIX = r"^\s*((re|fwd?|aw|wg)\s*:\s*)+"


def _gm_lookup(acct: dict, frm: str, msgid: str, tries: int = 1, wait: float = 3.0) -> dict:
    """The Gmail thread id (X-GM-THRID), References and Subject of the message with this Message-ID in
    the account's All Mail -> {"thrid", "refs", "subject"} or {}. Read-only; any failure -> {}.
    tries > 1 waits for a just-sent message to show up."""
    import imaplib
    import re as _re
    import time as _time
    from email import message_from_bytes
    pw = secret(acct.get("secret", "")) or secret(f"smtp:{acct['name']}")
    if not pw or not msgid:
        return {}
    try:
        m = imaplib.IMAP4_SSL(acct.get("host") or "imap.gmail.com", timeout=25)
        m.login(frm, pw)
        found = {}
        for n in range(max(1, tries)):
            if n:
                _time.sleep(wait)
            if m.select('"[Gmail]/All Mail"', readonly=True)[0] != "OK":
                break
            typ, data = m.uid("SEARCH", None, "HEADER", "Message-ID", f'"{msgid}"')
            if typ != "OK" or not data or not data[0]:
                continue
            uid = data[0].split()[-1]
            t2, d2 = m.uid("FETCH", uid, "(X-GM-THRID BODY.PEEK[HEADER.FIELDS (REFERENCES SUBJECT)])")
            if t2 != "OK" or not d2 or not isinstance(d2[0], tuple):
                continue
            th = _re.search(rb"X-GM-THRID (\d+)", d2[0][0])
            h = message_from_bytes(d2[0][1])
            found = {"thrid": th.group(1).decode() if th else "",
                     "refs": " ".join(str(h.get("References") or "").split()),
                     "subject": " ".join(str(h.get("Subject") or "").split())}
            break
        try:
            m.logout()
        except Exception:  # noqa: BLE001
            pass
        return found
    except Exception:  # noqa: BLE001
        return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="frm", required=True)
    ap.add_argument("--to", required=True, help="comma-separated")
    ap.add_argument("--subject", required=True)
    ap.add_argument("--body-file", required=True)
    ap.add_argument("--cc", default="")
    ap.add_argument("--in-reply-to", default="")
    ap.add_argument("--references", default="")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    frm = a.frm.strip().lower()
    if any(frm.endswith("@" + d) for d in FORBIDDEN_DOMAINS):
        print("REFUSED=forbidden_identity"); return 1
    acct = _account_for(frm)
    if not acct:
        print(f"REFUSED=unknown_identity {frm} (not in assistant/config.json mail.accounts)"); return 1
    tos = [t.strip() for t in a.to.split(",") if t.strip()]
    ccs = [t.strip() for t in a.cc.split(",") if t.strip()]
    if not tos or any("@" not in t for t in tos + ccs):
        print("REFUSED=bad_recipient"); return 1
    if any(any(t.lower().endswith("@" + d) for d in FORBIDDEN_DOMAINS) for t in tos + ccs):
        print("REFUSED=property_recipient"); return 1
    body = open(a.body_file, encoding="utf-8").read()
    if not body.strip():
        print("REFUSED=empty_body"); return 1

    import re as _re
    subject = a.subject
    irt, refs = a.in_reply_to.strip(), a.references
    if irt and not irt.startswith("<"):
        irt = f"<{irt.strip('<>')}>"
    if not irt and subject.strip().lower().startswith("re:") and not a.dry_run:
        # 2026-10-08: Sharon's thank-you was a "Re:" with no Message-ID on the draft, so Gmail showed it
        # as a new thread (the owner: "It should have been a reply"). Look the thread up in the mailbox.
        irt, refs = _find_thread(acct, frm, tos[0], subject)
    parent = {}
    if irt:
        # Gmail only threads a reply whose References chain and "Re: <same subject>" match the parent:
        # take both from the parent itself when the mailbox has it.
        parent = {} if a.dry_run else _gm_lookup(acct, frm, irt)
        chain = (refs or parent.get("refs") or "").split()
        refs = " ".join([r for r in chain if r != irt] + [irt])
        core = _re.sub(_RE_PREFIX, "", parent.get("subject") or subject, flags=_re.I).strip()
        if core:
            subject = "Re: " + core

    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = frm, ", ".join(tos), subject
    if ccs:
        msg["Cc"] = ", ".join(ccs)
    msg["Date"] = formatdate(localtime=True)
    mid = f"<{uuid.uuid4().hex}@{frm.split('@')[1]}>"
    msg["Message-ID"] = mid
    if irt:
        msg["In-Reply-To"] = irt
        msg["References"] = refs
    msg.set_content(body)

    if a.dry_run:
        print(f"DRY_RUN_OK from={frm} to={','.join(tos)} subject={subject[:60]!r} bytes={len(body)}")
        return 0
    pw = secret(f"smtp:{acct['name']}") or secret(acct.get("secret", ""))
    if not pw:
        print(f"NOT_CONFIGURED smtp:{acct['name']} (set with setup_creds.py mail {acct['name']})"); return 1
    # Three outcomes, and drain.py treats them differently:
    #   NOT_SENT ...            failed before the message was handed over: safe to retry
    #   SENT=1 <id>             the server accepted it
    #   DELIVERY_UNVERIFIED ... failed while handing it over: it may or may not have gone,
    #                           so it must NEVER be retried blind (a human checks Sent mail)
    # A failure while closing the connection AFTER acceptance is still SENT (it used to be
    # reported as an error, and the retry sent the email twice).
    stage, s = "connect", None
    try:
        s = smtplib.SMTP("smtp.gmail.com", 587, timeout=60)
        s.ehlo(); s.starttls(); s.ehlo()
        stage = "login"
        s.login(frm, pw)
        stage = _STAGE["at"] = "send"
        refused = s.send_message(msg, from_addr=frm, to_addrs=tos + ccs) or {}
        stage = _STAGE["at"] = "sent"
    except smtplib.SMTPAuthenticationError:
        print("LOGIN_REQUIRED smtp auth failed (app password?)"); return 1
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as e:
        print(f"NOT_SENT server refused the message: {type(e).__name__}: {e}"); return 1
    except Exception as e:
        if stage == "send":
            print(f"DELIVERY_UNVERIFIED {type(e).__name__}: {e}"); return 1
        print(f"NOT_SENT at {stage}: {type(e).__name__}: {e}"); return 1
    finally:
        try:
            if s is not None:
                s.quit()
        except Exception:
            try:
                s.close()
            except Exception:
                pass
    # the server can accept the message and still refuse SOME recipients: sent, but someone did not get it
    thread = ""
    if irt:
        # read the sent copy back: a reply that landed outside the parent's thread is reported, not hidden
        if not parent.get("thrid"):
            thread = " THREAD_UNVERIFIED=parent_not_in_mailbox"
        else:
            got = _gm_lookup(acct, frm, mid, tries=5).get("thrid", "")
            thread = (" THREAD_UNVERIFIED=sent_copy_not_found" if not got
                      else " THREAD=ok" if got == parent["thrid"]
                      else f" THREAD_MISMATCH=sent:{got},parent:{parent['thrid']}")
    print(f"SENT=1 {mid}" + (" PARTIAL_REFUSED=" + ",".join(sorted(refused)) if refused else "") + thread)
    return 0


if __name__ == "__main__":
    try:
        # piped stdout is cp1252 on Windows: an error message with a character it cannot encode
        # must not turn the outcome line itself into an exception
        sys.stdout.reconfigure(errors="backslashreplace")
    except Exception:
        pass
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:      # main() handles every SMTP outcome itself, so this is a last resort.
        # Once the message was being handed over it may have gone: never report that as
        # NOT_SENT, which drain.py would retry (a second copy of the email).
        if _STAGE["at"] in ("send", "sent"):
            print(f"DELIVERY_UNVERIFIED {type(e).__name__}: {ascii(str(e))}")
        else:
            print(f"NOT_SENT at setup: {type(e).__name__}: {ascii(str(e))}")
        sys.exit(1)
