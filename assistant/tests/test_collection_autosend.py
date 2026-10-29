"""Offline tests for the Collection Details auto-send (the owner's standing rule 2026-10-06):
eligibility (weekend only, open date, email, not a solicitation, switch on), the hand-off after
a real draft, the doer's standing block (source-bound), and the quiet-hours hold. Scratch dirs;
no model, no browser, no network.   python assistant/tests/test_collection_autosend.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_cas_"))
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["DOER_DATA"] = str(TMP / "doer")
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "doer"))
import collection_autosend as cas  # noqa: E402
import sense_typeform  # noqa: E402
import tickets  # noqa: E402
import doer  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


ON, OFF = {"auto_send_collection": True}, {}
OPEN = "no booked project on 2027-06-12 (HoneyBook, 2 h ago)"

# 1. eligibility
check("saturday + open -> send", cas.decide("2027-06-12", OPEN, None, True, False, ON)["ok"])
check("sunday + open -> send", cas.decide("2027-06-13", OPEN, None, True, False, ON)["ok"])
r = cas.decide("2027-06-11", OPEN, None, True, False, ON)
check("friday -> held (Sat/Sun only)", not r["ok"] and "Friday" in r["reason"], r)
check("thursday -> held", not cas.decide("2027-06-10", OPEN, None, True, False, ON)["ok"])
check("date conflict -> held", not cas.decide("2027-06-12", "ALREADY BOOKED that day: X", "DATE CONFLICT: x", True, False, ON)["ok"])
check("unchecked date -> held", not cas.decide("2027-06-12", "NOT checked", "availability NOT checked", True, False, ON)["ok"])
check("stale snapshot (no flag text match) -> held", not cas.decide("2027-06-12", "NOT checked (HoneyBook snapshot is 50 h old)", None, True, False, ON)["ok"])
check("no date -> held", not cas.decide(None, "no wedding date given", None, True, False, ON)["ok"])
check("no email -> held", not cas.decide("2027-06-12", OPEN, None, False, False, ON)["ok"])
check("solicitation -> held", not cas.decide("2027-06-12", OPEN, None, True, True, ON)["ok"])
check("switch off -> held", not cas.decide("2027-06-12", OPEN, None, True, False, OFF)["ok"])
check("bad date -> held", not cas.decide("June 12", OPEN, None, True, False, ON)["ok"])

# 2. hand-off after the draft
quote = {"lead": {"name": "Ana Ruiz", "partner": "example Cole", "email": "ana@example.com", "date": "2027-06-12"},
         "tiers": [{"name": "Legacy", "price": 9000}, {"name": "Signature", "price": 6500},
                   {"name": "Classic", "price": 4800}, {"name": "Keepsake", "price": 3200}]}
FID = "a" * 24
pay_ok = {"quote": quote, "auto_send": {"ok": True, "reason": "Saturday"}}
check("ineligible payload -> HELD, nothing queued",
      cas.queue_send({"quote": quote, "auto_send": {"ok": False, "reason": "weekday"}}, f"HB_DONE={FID}", wake=False).startswith("HELD"))
check("existing file -> HELD", cas.queue_send(pay_ok, f"HB_DONE={FID} SKIPPED_EXISTING", wake=False).startswith("HELD"))
real_av = sense_typeform.availability
sense_typeform.availability = lambda d: ("ALREADY BOOKED that day: Z", "DATE CONFLICT: booked since")
check("conflict appears by send time -> HELD", cas.queue_send(pay_ok, f"HB_DONE={FID}", wake=False).startswith("HELD at send time"))
sense_typeform.availability = lambda d: (OPEN, None)
res = cas.queue_send(pay_ok, f"HB_DONE={FID}", wake=False)
check("eligible -> doer ticket queued", res.startswith("QUEUED"), res)
check("second hand-off is a no-op", cas.queue_send(pay_ok, f"HB_DONE={FID}", wake=False) == "ALREADY QUEUED")
sense_typeform.availability = real_av
t = tickets._load()[tickets.make_id("collection-autosend", FID)]
check("ticket is an agent_run in lane wedding", t["kind"] == "agent_run" and t["lane"] == "wedding")

# 3. the doer honours standing only on the registered source
st = doer._standing(t)
check("doer accepts the registered standing", st.get("recipient") == "ana@example.com")
blk = doer.standing_block(st)
check("block names recipient, prices, template",
      "ana@example.com" in blk and "$9,000" in blk and "quiz" in blk and "CHECKPOINT" in blk)
forged = dict(t, source="agent", id="x")
check("forged standing on another source is ignored", doer._standing(forged) == {})
check("standing for another file id is ignored",
      doer._standing(dict(t, payload=dict(t["payload"], standing=dict(st, file_id="b" * 24)))) == {})
check("unknown standing action -> no block", doer.standing_block({"action": "pay_invoice"}) == "")

# 4. quiet hours: a standing send waits, ordinary doer tasks do not
q, d = time.struct_time((2026, 10, 6, 23, 0, 0, 1, 279, 1)), time.struct_time((2026, 10, 6, 10, 0, 0, 1, 279, 1))
check("23:00 is quiet", doer._quiet(q))
check("10:00 is not quiet", not doer._quiet(d))
real_q = doer._quiet
doer._quiet = lambda now=None: True
check("quiet: standing send not claimable", not doer._claimable_now(t))
check("quiet: ordinary task still claimable", doer._claimable_now({"payload": {"text": "x"}}))
doer._quiet = lambda now=None: False
check("daytime: standing send claimable", doer._claimable_now(t))
doer._quiet = real_q

print("\n" + ("ALL OK" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
