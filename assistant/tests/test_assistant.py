"""Run:  python assistant/tests/test_assistant.py     (stdlib only, uses a temp data dir)"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))   # never the live ~/.claude logs
import sys
import tempfile
import threading
import unittest
from unittest import mock
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import alib  # noqa: E402

_tmp = Path(tempfile.mkdtemp(prefix="asst_test_"))
alib.DATA = _tmp                      # redirect BEFORE tickets binds its paths
import tickets  # noqa: E402

tickets.DATA = _tmp
tickets.TICKETS = _tmp / "tickets.json"


def item(n, lane="personal"):
    return {"source": "test", "source_id": f"m{n}", "lane": lane, "kind": "email", "title": f"t{n}", "summary": "", "payload": {}}


class QueueTests(unittest.TestCase):
    def setUp(self):
        for f in _tmp.glob("*"):
            if f.is_file():
                f.unlink()

    def test_dedup_by_id(self):
        self.assertEqual(tickets.add_many([item(1), item(2)], "t"), 2)
        self.assertEqual(tickets.add_many([item(2), item(3)], "t"), 1)
        self.assertEqual(tickets.pending(), 3)

    def test_unknown_lane_falls_back(self):
        tickets.add_many([item(1, lane="bogus")], "t")
        self.assertEqual(list(tickets._load().values())[0]["lane"], "personal")

    def test_claim_is_exclusive_and_lane_scoped(self):
        tickets.add_many([item(1, "wedding"), item(2, "money"), item(3, "wedding")], "t")
        got = tickets.claim(lane="wedding")
        self.assertEqual(len(got), 2)
        self.assertEqual(tickets.claim(lane="wedding"), [])
        self.assertEqual(tickets.pending(), 1)

    def test_stale_claim_becomes_claimable(self):
        tickets.add_many([item(1)], "t")
        tid = tickets.claim()[0]["id"]
        db = tickets._load(); db[tid]["updated"] = "2020-01-01T00:00:00Z"
        alib.save_json(tickets.TICKETS, db)
        self.assertEqual(tickets.pending(), 1)

    def test_transitions_guarded(self):
        tickets.add_many([item(1)], "t")
        tid = tickets.claim()[0]["id"]
        self.assertIsNotNone(tickets._transition(tid, "staged", "op", "n", {"claimed", "new"}))
        self.assertIsNone(tickets._transition(tid, "staged", "op", "n", {"claimed", "new"}))   # already staged
        self.assertIsNone(tickets._transition("nope", "done", "op"))
        self.assertEqual(tickets.pending(), 0)

    def test_concurrent_writers_lose_nothing(self):
        def work(k):
            tickets.add_many([item(f"{k}-{i}") for i in range(5)], f"w{k}")
        th = [threading.Thread(target=work, args=(k,)) for k in range(6)]
        [t.start() for t in th]; [t.join() for t in th]
        self.assertEqual(len(tickets._load()), 30)

    def test_stale_lock_is_broken(self):
        lock = _tmp / ".queue.lock"
        lock.write_text("{}")
        import os, time
        os.utime(lock, (time.time() - 5000, time.time() - 5000))
        self.assertEqual(tickets.add_many([item(1)], "t"), 1)


class IcsTests(unittest.TestCase):
    def test_parse_folded_and_allday(self):
        sys.modules.setdefault("keyring", type(sys)("keyring"))     # parse_ics needs no secrets
        import sense_local
        d = (date.today() + timedelta(days=3)).strftime("%Y%m%d")
        ics = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:a1\r\nDTSTART;VALUE=DATE:%s\r\nSUMMARY:Glenrock wedd\r\n ing shoot\r\n"
               "LOCATION:example\\, CA\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:a2\r\nDTSTART;TZID=America/Los_Angeles:%sT143000\r\n"
               "SUMMARY:Dentist\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n") % (d, d)
        ev = sense_local.parse_ics(ics)
        self.assertEqual(ev[0]["title"], "Glenrock wedding shoot")
        self.assertEqual(ev[0]["location"], "example, CA")
        self.assertIsNone(ev[0]["time"])
        self.assertEqual(ev[1]["time"], "14:30")

    def test_rrule_expansion(self):
        sys.modules.setdefault("keyring", type(sys)("keyring"))
        import sense_local
        from datetime import date as D
        ws, we = D(2026, 9, 22), D(2026, 10, 6)                    # a 14-day window, Tue..Tue
        ex = sense_local.expand_rrule
        # weekly on Mon+Wed, started long ago, one date excluded
        got = ex(D(2026, 1, 5), "FREQ=WEEKLY;BYDAY=MO,WE", ws, we, exdates={D(2026, 9, 28)})
        self.assertEqual(got, [D(2026, 9, 23), D(2026, 9, 30), D(2026, 10, 5)])
        # every 2 days with COUNT: 3 occurrences total, only the ones in-window
        self.assertEqual(ex(D(2026, 9, 21), "FREQ=DAILY;INTERVAL=2;COUNT=3", ws, we), [D(2026, 9, 23), D(2026, 9, 25)])
        # monthly on the 31st skips short months; UNTIL respected
        self.assertEqual(ex(D(2026, 8, 31), "FREQ=MONTHLY;UNTIL=20261001", ws, we), [])
        self.assertEqual(ex(D(2026, 7, 1), "FREQ=MONTHLY", ws, we), [D(2026, 10, 1)])
        self.assertEqual(ex(D(2025, 9, 25), "FREQ=YEARLY", ws, we), [D(2026, 9, 25)])
        # long-running series reach the window (no occurrence ceiling in the way)
        self.assertEqual(len(ex(D(2000, 1, 1), "FREQ=DAILY", ws, we)), 15)
        self.assertEqual(ex(D(1990, 9, 22), "FREQ=WEEKLY;INTERVAL=2", ws, we), [D(2026, 10, 3)])   # brute-force checked
        self.assertEqual(ex(D(1990, 10, 3), "FREQ=MONTHLY;INTERVAL=3", ws, we), [D(2026, 10, 3)])
        # unsupported rule -> first date only, no crash
        self.assertEqual(ex(D(2026, 9, 24), "FREQ=HOURLY", ws, we), [D(2026, 9, 24)])
        # parse_ics with a window expands; without one it lists the first date
        ics = ("BEGIN:VEVENT\r\nUID:r1\r\nDTSTART:20260101T090000\r\nRRULE:FREQ=WEEKLY;BYDAY=TU\r\n"
               "EXDATE:20260929T090000\r\nSUMMARY:Standup\r\nEND:VEVENT")
        ev = sense_local.parse_ics(ics, ws, we)
        self.assertEqual([e["date"] for e in ev], ["2026-09-22", "2026-10-06"])
        self.assertTrue(all(e["recurring"] and e["time"] == "09:00" for e in ev))
        self.assertEqual([e["date"] for e in sense_local.parse_ics(ics)], ["2026-01-01"])


class FakeImap:
    """Just enough IMAP for mail.py: uid SEARCH/FETCH over canned messages, APPEND to Drafts."""
    def __init__(self, msgs):
        self.msgs, self.drafts, self.selected, self.log = msgs, [], None, []
        self.archive, self.fetched = dict(msgs), []          # "[Gmail]/All Mail" keeps everything

    def select(self, box, readonly=False):
        self.log.append(("select", box, readonly)); self.selected = box
        return "OK", [b"1"]

    def uid(self, cmd, *args):
        if cmd == "SEARCH" and args[0] == "X-GM-RAW":
            return "OK", [" ".join(str(u) for u in sorted(self.msgs)).encode()]
        if cmd == "SEARCH" and args[0] == "HEADER" and "All Mail" in str(self.selected):   # find an archived mail
            hits = [u for u, raw in self.archive.items() if args[2].encode() in raw]
            return "OK", [" ".join(hits).encode()]
        if cmd == "SEARCH" and args[0] == "HEADER":          # draft verification by Message-ID
            hit = any(args[2] in d for d in self.drafts)
            return "OK", [b"7" if hit else b""]
        if cmd == "FETCH":
            self.fetched.append(args[1] if len(args) > 1 else "")
            src = self.archive if "All Mail" in str(self.selected) else self.msgs
            if args[0] not in src:
                return "NO", [None]
            raw = src[args[0]]
            hdr, _, body = raw.partition(b"\r\n\r\n")
            return "OK", [(b"HEADER", hdr + b"\r\n\r\n"), (b"TEXT", body), (b"BODY[]", raw)]
        raise AssertionError(cmd)

    def append(self, box, flags, date, data):
        self.log.append(("append", box, flags)); self.drafts.append(data.decode())
        return "OK", [b"[APPENDUID 1 7]"]

    def logout(self):
        return "BYE", []


def raw_mail(frm, subj, body, mid, extra=""):
    return (f"From: {frm}\r\nTo: owner4@example.invalid\r\nSubject: {subj}\r\nDate: Tue, 22 Sep 2026 08:00:00 -0700\r\n"
            f"Message-ID: {mid}\r\n{extra}Content-Type: text/plain\r\n\r\n{body}\r\n").encode()


class MailSensorTests(unittest.TestCase):
    def setUp(self):
        for f in _tmp.glob("*"):
            if f.is_file():
                f.unlink()
        sys.modules.setdefault("keyring", type(sys)("keyring"))
        import mail
        self.mail = mail
        mail.DATA, mail.SEEN, mail.STATUS = _tmp, _tmp / "seen.json", _tmp / "status.json"
        mail.OUT = _tmp / "out"; (mail.OUT / "drafts").mkdir(parents=True, exist_ok=True)
        self.imap = FakeImap({
            "101": raw_mail("Ana <ana@example.com>", "Wedding videography inquiry", "Hi! Are you free Oct 10?", "<a@x>"),
            "102": raw_mail("SoCal Edison <p0fae7a@example.invalid>", "Your bill is ready", "Amount due $120", "<b@x>", "List-Unsubscribe: <mailto:p4eafae@example.invalid>\r\n"),
            "103": raw_mail("Mailer-Daemon@google.com", "Delivery failure", "bounce", "<c@x>"),
            "104": raw_mail("Mom <mom@example.com>", "Sunday?", "Dinner at 6", "<d@x>"),
        })
        mail._connect = lambda acct: self.imap
        # the fake IMAP serves ONE mailbox; the second (example) account was added
        # 2026-09-30 and would otherwise double every ticket in this test
        _real_config = alib.config
        mail.config = lambda: {**_real_config(), "mail": {**_real_config()["mail"],
                               "accounts": [a for a in _real_config()["mail"]["accounts"] if a["name"] == "personal"]}}

    def test_sense_routes_lanes_and_tracks_seen(self):
        self.mail.sense()
        db = tickets._load()
        by_subj = {t["payload"]["subject"]: t for t in db.values()}
        self.assertEqual(set(by_subj), {"Wedding videography inquiry", "Your bill is ready", "Sunday?"})   # daemon skipped
        self.assertEqual(by_subj["Wedding videography inquiry"]["lane"], "wedding")
        self.assertEqual(by_subj["Your bill is ready"]["lane"], "money")
        self.assertTrue(by_subj["Your bill is ready"]["payload"]["bulk"])
        self.assertEqual(by_subj["Sunday?"]["lane"], "personal")
        self.assertFalse(by_subj["Sunday?"]["payload"]["bulk"])
        self.assertIn(("select", "INBOX", True), self.imap.log)             # never marks mail read
        self.assertEqual(self.mail.sense.__name__, "sense")
        self.mail.sense()                                                    # second pass: all seen
        self.assertEqual(len(tickets._load()), 3)
        seen = alib.load_json(self.mail.SEEN, {})
        self.assertEqual(seen["personal"], ["101", "102", "103", "104"])

    def test_draft_goes_only_to_original_sender_and_is_verified(self):
        self.mail.sense()
        tid = next(t["id"] for t in tickets._load().values() if t["payload"]["uid"] == "101")
        (self.mail.OUT / "drafts" / f"{tid}.txt").write_text("Hi Ana, thanks for reaching out! [owner1: confirm date]", encoding="utf-8")
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.mail.draft(tid, f"{tid}.txt")
        self.assertIn("DRAFT_OK", buf.getvalue())
        self.assertEqual(len(self.imap.drafts), 1)
        d = self.imap.drafts[0]
        self.assertIn("To: ana@example.com", d)
        self.assertIn("Subject: Re: Wedding videography inquiry", d)
        self.assertIn("In-Reply-To: <a@x>", d)
        self.assertEqual([x for x in self.imap.log if x[0] == "append"], [("append", '"[Gmail]/Drafts"', "(\\Draft)")])
        with self.assertRaises(SystemExit):                                   # path traversal refused
            self.mail.draft(tid, "../evil.txt")

    def _draft(self, tid):
        import io, contextlib
        (self.mail.OUT / "drafts" / f"{tid}.txt").write_text("Thanks! [owner1: confirm date]", encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.mail.draft(tid, f"{tid}.txt")
        return buf.getvalue()

    def test_reply_to_header_never_steers_the_draft(self):
        self.imap.msgs["105"] = self.imap.archive["105"] = raw_mail(
            "Ana <ana@example.com>", "Quick question", "Call me", "<e@x>", "Reply-To: attacker@evil.example\r\n")
        self.mail.sense()
        tid = next(t["id"] for t in tickets._load().values() if t["payload"]["uid"] == "105")
        out = self._draft(tid)
        self.assertIn("To: ana@example.com", self.imap.drafts[-1])
        self.assertNotIn("attacker@evil.example", self.imap.drafts[-1])
        self.assertIn("reply-to header (attacker@evil.example) ignored", out)

    def test_draft_refused_when_the_uid_now_points_at_another_message(self):
        self.mail.sense()
        tid = next(t["id"] for t in tickets._load().values() if t["payload"]["uid"] == "101")
        # the mailbox was renumbered: uid 101 is now someone else's mail, and the original is gone
        self.imap.msgs["101"] = raw_mail("Stranger <x@other.example>", "Hello", "hi", "<zzz@x>")
        del self.imap.archive["101"]
        with self.assertRaises(SystemExit):
            self._draft(tid)
        self.assertEqual(self.imap.drafts, [])

    def test_archived_mail_is_found_by_message_id(self):
        self.mail.sense()
        tid = next(t["id"] for t in tickets._load().values() if t["payload"]["uid"] == "104")
        del self.imap.msgs["104"]                       # the owner archived it: gone from INBOX, still in All Mail
        self.assertIn("DRAFT_OK", self._draft(tid))
        self.assertIn("To: mom@example.com", self.imap.drafts[-1])

    def test_snippet_is_readable_text_and_one_bad_mail_blocks_nothing(self):
        multipart = (b"From: Vendor <billing@vendor.example>\r\nTo: j@x\r\nSubject: Statement\r\nMessage-ID: <mp@x>\r\n"
                     b"MIME-Version: 1.0\r\nContent-Type: multipart/alternative; boundary=\"BB\"\r\n\r\n"
                     b"--BB\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: base64\r\n\r\n"
                     b"WW91ciBpbnZvaWNlIGlzIGR1ZSBPY3RvYmVyIDI1Lg==\r\n--BB--\r\n")
        self.imap.msgs["106"] = self.imap.archive["106"] = multipart
        self.imap.msgs["107"] = self.imap.archive["107"] = raw_mail("Bob <bob@example.com>", "After the bad one", "ok", "<g@x>")
        real = self.mail._sense_one

        def flaky(m, uid, *a):
            if uid == "106x":
                raise ValueError("unparseable")
            return real(m, uid, *a)
        self.imap.msgs["106x"] = raw_mail("Bad <bad@example.com>", "Broken", "x", "<bad@x>")
        self.mail._sense_one = flaky
        # uids must sort as ints in the real code: use a numeric uid for the broken one
        self.imap.msgs["108"] = self.imap.msgs.pop("106x")
        self.mail._sense_one = lambda m, uid, *a: (_ for _ in ()).throw(ValueError("unparseable")) if uid == "108" else real(m, uid, *a)
        self.mail.sense()
        self.mail._sense_one = real
        by_subj = {t["payload"]["subject"]: t for t in tickets._load().values()}
        self.assertIn("Your invoice is due October 25.", by_subj["Statement"]["summary"])   # decoded text, not base64
        self.assertNotIn("WW91ciBpbnZvaWNl", by_subj["Statement"]["summary"])
        self.assertIn("After the bad one", by_subj)                                        # the bad mail blocked nothing
        self.assertIn("108", alib.load_json(self.mail.SEEN, {})["personal"])               # and is not retried forever
        self.assertIn("unparsed=1", alib.load_json(self.mail.STATUS, {})["accounts"]["personal"])
        self.assertTrue(all("PEEK" in f for f in self.imap.fetched if f))                   # never marks mail read

    def test_default_lane_of_the_account(self):
        self.assertEqual(self.mail._lane_for("nothing matches here", "wedding"), "wedding")
        self.assertEqual(self.mail._lane_for("nothing matches here"), "personal")


class HbSnapshotTests(unittest.TestCase):
    """A connector timeout must not flag HoneyBook as down (2026-10-07: false 're-auth' cards)."""
    def test_transient_timeout_is_tolerated_until_it_repeats(self):
        import hb_snapshot as hs
        msg = "no snapshot in the transcript (FAILED: HoneyBook MCP server connection timed out after 30000ms)"
        self.assertEqual(hs.failure_fields(msg, 0)["error"], "")
        self.assertEqual(hs.failure_fields(msg, 0)["last_error"], msg)
        self.assertEqual(hs.failure_fields(msg, None)["fails"], 1)
        self.assertEqual(hs.failure_fields(msg, hs.TRANSIENT_GRACE - 1)["error"], msg)

    def test_real_failures_report_at_once(self):
        import hb_snapshot as hs
        for msg in ("stages empty or malformed", "no snapshot in the transcript (FAILED: 401 Unauthorized)"):
            self.assertEqual(hs.failure_fields(msg, 0)["error"], msg)

    def test_oversized_result_saved_to_file_is_followed(self):
        """Both wordings of claude's 'too big, saved to a file' note (the newer one made the
        connector look de-authorised for 3 days, 2026-10-09..11)."""
        import json
        import hb_snapshot as hs
        snap = {"ts": "2026-10-12T02:10:38Z", "counts": {"active": 1}, "stages": [{"stage_id": "s", "category": "lead"}],
                "projects": [{"id": "p", "stage_id": "s"}], "money": {}, "tasks": []}
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "mcp-honeybook-take_action_read_only-1.txt"
            f.write_text(json.dumps(snap), encoding="utf-8")
            for note in (f"Error: result (63,765 characters across 1 line) exceeds maximum allowed tokens. Output has been saved to {f}.\nFormat: Plain text",
                         f"Full output saved to: {f}"):
                call = {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": hs.ALLOWED}]}}
                ev = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": note}]}}
                got = hs.extract(json.dumps(call) + "\n" + json.dumps(ev))
                self.assertEqual((got or {}).get("ts"), snap["ts"], note[:60])
                self.assertEqual(hs.validate(got, {}), "")

    def test_only_the_honeybook_tool_result_counts(self):
        """2026-10-23..25: the refresher reported 'no snapshot' quoting another MCP server's tool
        description (a:prepare_bounded_process_capture). Only the HoneyBook call's own result is read,
        and a miss names its reason instead of the raw transcript tail."""
        import json
        import hb_snapshot as hs
        snap = {"ts": "2026-10-25T16:00:00Z", "counts": {"active": 1}, "stages": [{"stage_id": "s", "category": "lead"}],
                "projects": [{"id": "p", "stage_id": "s"}], "money": {}, "tasks": []}
        decoy = dict(snap, ts="1999-01-01T00:00:00Z")
        other_tools = json.dumps([{"name": "mcp__rea__prepare_bounded_process_capture",
                                   "description": "Choose a command... (MCP)"}, {"name": "mcp__playwright__browser_click"}])
        init = {"type": "system", "subtype": "init", "tools": ["mcp__rea__prepare_bounded_process_capture"],
                "mcp_servers": [{"name": "rea", "status": "connected"}, {"name": "honeybook", "status": "connected"}]}
        search = [{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "ts1", "name": "ToolSearch"}]}},
                  {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "ts1",
                                                            "content": other_tools + json.dumps(decoy)}]}}]
        hb = [{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "hb1", "name": hs.ALLOWED}]}},
              {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "hb1",
                                                        "content": [{"type": "text", "text": json.dumps(snap)}]}]}}]
        done = {"type": "result", "subtype": "success", "result": "DONE"}
        lines = lambda evs: "\n".join(json.dumps(e) for e in evs)
        self.assertEqual(hs.extract(lines([init, *search, *hb, done]))["ts"], snap["ts"])      # decoy skipped
        self.assertIsNone(hs.extract(lines([init, *search, done])))                           # no HoneyBook call
        with tempfile.TemporaryDirectory() as d, mock.patch.object(hs, "SNAP", Path(d) / "hb.json"):
            ok, msg = hs.install(lines([init, *search, done, init]))                          # tail = tool list
            self.assertFalse(ok)
            self.assertIn("HoneyBook tool was never called", msg)
            self.assertNotIn("prepare_bounded_process_capture", msg)
            down = dict(init, mcp_servers=[{"name": "honeybook", "status": "failed"}])
            self.assertIn("HoneyBook connector failed", hs.install(lines([down, done]))[1])
            self.assertTrue(hs.install(lines([init, *search, *hb, done]))[0])


class SendGateTests(unittest.TestCase):
    def test_mail_module_has_no_send_path(self):
        src = (Path(__file__).resolve().parent.parent / "scripts" / "mail.py").read_text(encoding="utf-8")
        code = src.split('"""', 2)[2]                                # skip the docstring
        for banned in ("smtplib", "SMTP(", "sendmail", "send_message"):
            self.assertNotIn(banned, code)
        self.assertIn("readonly=True", code)


if __name__ == "__main__":
    unittest.main(verbosity=1)
