"""billcheck.py - a "was this bill or fee paid" job goes to the provider's own site, never a bank.

Oct 30 2026: the Cox fee check was handed to the doer as "sign in to the account", and it signed in to
Chase with a vault login. Payments are never read from bank sites. So before a `do` job is queued,
code looks at it: a paid-or-not check names the provider's own site and says outright not to sign in
to any bank, credit union or card site; and when the provider's login is marked stale in the doer's
playbooks (sites.json: the site said the login is wrong), the job is not sent at all and the owner
gets one line that the login needs updating instead (the Setup page itself is never named in Chat:
chatfirst.py drops any sentence that sends an owner to the computer).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # assistant/

PAID = re.compile(r"\b(paid|pay|payment|payments|receipt)\b", re.I)
BILLISH = re.compile(r"\b(fee|fees|bill|bills|charge|charges|invoice|premium|toll|tolls|registration|account)\b", re.I)
# provider word -> its own site (the ones the owners pay; a domain written in the job text wins)
PROVIDERS = {"cox": ("Cox", "cox.com"), "etags": ("eTags", "etags.com"), "e-tags": ("eTags", "etags.com"),
             "dmv": ("the DMV", "dmv.ca.gov"), "fastrak": ("FasTrak", "thetollroads.com"),
             "geico": ("GEICO", "geico.com"), "progressive": ("Progressive", "progressive.com"),
             "state farm": ("State Farm", "statefarm.com"), "allstate": ("Allstate", "allstate.com"),
             "sce": ("SCE", "sce.com"), "socalgas": ("SoCalGas", "socalgas.com"), "verizon": ("Verizon", "verizon.com")}
BANKS = re.compile(r"\b(chase|wells ?fargo|bank of america|bofa|citi(?:bank)?|capital ?one|amex|american express|discover|"
                   r"us ?bank|credit union|navy federal|schwab|ally)\b|\bbank\b", re.I)
DOMAIN = re.compile(r"\b((?:[a-z0-9-]+\.)+(?:com|net|org|gov|us|io|ca\.gov))\b", re.I)
RULE = ("Read the payment on the provider's own site{site}. Do not sign in to any bank, credit union or card site "
        "for this; payments are never read from bank sites. If the provider's site "
        "does not show it, report that back instead.")


def is_bill_check(text: str) -> bool:
    t = str(text or "")
    return bool(PAID.search(t) and BILLISH.search(t))


def provider(text: str) -> tuple:
    """(name, domain) of the provider the job is about; ("", "") when it names none."""
    t = str(text or "")
    for m in DOMAIN.finditer(t):
        d = m.group(1).lower().removeprefix("www.")
        if not BANKS.search(d):
            return d.split(".")[0].title(), d
    low = t.lower()
    for word, (name, dom) in PROVIDERS.items():
        if re.search(r"\b" + re.escape(word) + r"\b", low):
            return name, dom
    return "", ""


def _sites() -> dict:
    data = Path(os.environ["DOER_DATA"]) if os.environ.get("DOER_DATA") else ROOT / "data" / "doer"
    try:
        return json.loads((data / "sites.json").read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001  no playbooks yet: nothing is known to be stale
        return {}


def _base(host: str) -> str:
    parts = str(host or "").lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else str(host or "").lower()


def stale(domain: str) -> bool:
    """The vault login for this site is marked stale: flagged so, or its last sign-in said the login is wrong."""
    if not domain:
        return False
    for host, pb in _sites().items():
        if _base(host) == _base(domain) and isinstance(pb, dict):
            last = str(pb.get("last_result") or "")
            if pb.get("stale") or "needs updating" in last or "login is wrong" in last:
                return True
    return False


class Stale(Exception):
    """The provider's login needs updating; str() is the one line for the owner."""


def check(text: str) -> str:
    """The job text to queue; raises Stale instead of sending a job that would need a dead login."""
    job, line = shape(text)
    if not job:
        raise Stale(line)
    return job


def shape(text: str) -> tuple:
    """(job text to queue or "", one line for the owner or ""). Anything that is not a paid-or-not check
    passes through unchanged."""
    if not is_bill_check(text):
        return text, ""
    name, dom = provider(text)
    if stale(dom):
        return "", f"I didn't check that payment: the {name} login I have is out of date, so it needs updating first."
    return text.rstrip() + "\n\n" + RULE.format(site=f" ({dom})" if dom else ""), ""
