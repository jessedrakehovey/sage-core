"""Offline tests for the ad scout's weekly search-terms report (ads/scripts/search_terms.py, 2026-10-25):
a fixture googleAds:search response -> parse -> candidate negatives, the proposal section, the panel
line, and a grep proving the new code never calls googleAds:mutate.
    python assistant/tests/test_search_terms.py
"""
import re
import sys
from pathlib import Path

ADS = Path(__file__).resolve().parent.parent.parent / "ads" / "scripts"
sys.path.insert(0, str(ADS))
import ingest  # noqa: E402
import search_terms as ST  # noqa: E402
from build_google import NEGATIVES  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def term(t, clicks, cost_usd, conv=0, camp="24313750485"):
    return {"searchTermView": {"searchTerm": t}, "campaign": {"id": camp},
            "metrics": {"clicks": str(clicks), "costMicros": str(int(cost_usd * 1e6)), "conversions": conv}}


def loc(inside, clicks, cost_usd):
    return {"userLocationView": {"countryCriterionId": "2840", "targetingLocation": inside},
            "campaign": {"id": "24313750485"}, "metrics": {"clicks": str(clicks), "costMicros": str(int(cost_usd * 1e6))}}


TERMS = [
    term("wedding photographer example", 3, 6.10),
    term("example wedding videographer", 9, 22.40),            # our keyword, no lead yet: never flagged
    term("example wedding videographer", 2, 4.00, camp="24319061960"),
    term("wedding videographer jobs", 1, 2.20),                       # already blocked by NEGATIVES 'jobs'
    term("los angeles wedding videographer", 1, 3.50),
    term("example wedding videographer los angeles", 1, 3.00),  # names an in-area place too
    term("wedding photo booth rental", 1, 1.80),                      # rental is a negative already
    term("wedding dj santa Marisol", 1, 2.00),
    term("wedding photographer and videographer example", 2, 4.00),  # wants video: not another vendor
    term("example-org", 2, 0.0),                                      # free click: never flagged
    term("best wedding films", 2, 7.25),                              # $5+ and no lead
    term("montecito wedding film", 2, 9.00, conv=1),                  # converted: never flagged
]
LOCS = [loc(True, 18, 50.0), loc(False, 6, 15.0)]

doc = {"days": 30, **ingest.parse_search_terms(TERMS, LOCS)}
check("parse: costMicros -> dollars", doc["terms"][0]["cost"] == 6.10, doc["terms"][0])
check("parse: targetingLocation -> inside", [l["inside"] for l in doc["locations"]] == [True, False], doc["locations"])

cands = ST.candidates(doc)
by = {c["term"]: c for c in cands}
check("'wedding photographer example' flagged as another vendor",
      "wedding photographer example" in by and "another vendor" in by["wedding photographer example"]["reasons"][0], cands)
check("'example wedding videographer' not flagged", "example wedding videographer" not in by, cands)
check("existing negative not repeated ('... jobs', '... rental')",
      "wedding videographer jobs" not in by and "wedding photo booth rental" not in by, cands)
check("outside-area place flagged", "outside the area" in by.get("los angeles wedding videographer", {}).get("reasons", [""])[0], cands)
check("in-area place wins over an outside one", "example wedding videographer los angeles" not in by, cands)
check("dj flagged as another vendor", "wedding dj santa Marisol" in by, cands)
check("photographer + videographer search kept", "wedding photographer and videographer example" not in by, cands)
check("$5+ with no lead flagged", by.get("best wedding films", {}).get("reasons") == ["$7.25 spent, no lead"], cands)
check("free click and converted term not flagged", "example-org" not in by and "montecito wedding film" not in by, cands)
check("expected candidate list exactly", sorted(by) == sorted(
    ["wedding photographer example", "los angeles wedding videographer", "wedding dj santa Marisol", "best wedding films"]), sorted(by))
check("no candidate is already covered by NEGATIVES", not any(ST.blocked(ST.words(c["term"]), NEGATIVES) for c in cands))

check("outside share = 6/24", abs(ST.outside_share(doc) - 0.25) < 1e-9, ST.outside_share(doc))
line = ST.summary(doc)
check("panel line", line == "4 searches worth blocking; 25% of clicks from people outside the area", line)
md = ST.to_markdown(doc)
check("proposal section rendered", "## Search terms (last 30 days)" in md and "| wedding photographer example |" in md
      and "build_google.py --update" in md, md)
check("no report -> empty section and line", ST.to_markdown({}) == "" and ST.summary({}) == "")

# Read-only proof: the new code never calls googleAds:mutate (build_google.py is the only writer).
src_st = (ADS / "search_terms.py").read_text(encoding="utf-8")
src_in = (ADS / "ingest.py").read_text(encoding="utf-8")
fn = re.search(r"def pull_search_terms\(.*?(?=\ndef )", src_in, re.S).group(0)
check("search_terms.py makes no mutate call", "mutate" not in src_st.replace("NEGATIVES", ""), "")
check("ingest.pull_search_terms uses googleAds:search only", "googleAds:search" in fn and "mutate" not in fn)
check("ingest.py has no mutate at all", "mutate" not in src_in)

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
