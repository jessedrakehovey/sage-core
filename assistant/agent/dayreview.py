"""dayreview.py - the day, graded ask by ask, and a review that acts instead of noting (2026-11-14).

An owner, 2026-11-14: "Does it check itself daily for what it could have done better and where it failed? It doesn't
seem to be good enough." It did check, several times a day, and the checking changed little, for reasons the stores
already showed:

  * nothing graded the owners' own asks. The review read the chat as prose; no line said "this ask was done, this
    one was late, this one they had to say twice".
  * the causes were written down and never read: 51 of 61 failures in a week were classed "other" (scorecard.py).
  * a fix the review asked for went to the back of a queue of three hundred, oldest first; 25 of them were never
    started in a week, and each was then counted as a failed build.
  * the same finding came back every pass and was answered with "already waiting on the owners".

This module is the code half of the review. Zero-LLM, read-only over the transcript, the outcome ledger and the
corrections ledger; the only things it writes are its own record, a build request, and the build queue's ranking.

  grade(day)          every ask an owner typed that day -> done | late | open | not done | no answer | repeated,
                      with the cause the ledger wrote for it
  causes(days)        this week's failures by cause, with an example each
  prioritize(key, n)  the build queue's ranking (builder.pick reads it): a recurring fault, a review-filed fix that
                      slipped and the day's worst asks go first, whatever their age
  run(day)            grade, rank the day's worst three and file each as ONE build request (a code fix with a test,
                      never a rule), bump the fixes that were never started, count how many runs in a row each
                      standing problem has been seen and act on the third (a build request with the evidence),
                      record one row
  dream_rows()        the lines the nightly review reads, so it proposes what would have PREVENTED the day's misses
                      and never files a second request for one this module already filed

Nothing here speaks in Chat: the record is `data/agent/dayreview.jsonl`, the work log gets one line.

  python assistant/agent/dayreview.py run [--day YYYY-MM-DD] [--dry-run]
  python assistant/agent/dayreview.py status | grade [--day YYYY-MM-DD] | priorities
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

sys.path.insert(0, str(core.AGENT.parent / "scripts"))

STORE = core.DATA / "dayreview.jsonl"
STATE = core.DATA / "dayreview_state.json"
PRIORITY = core.DATA / "build_priority.json"
CORRECTIONS = core.DATA / "corrections.jsonl"

BURST_S = 180               # lines an owner types within this long in one thread are one ask
ASK_WINDOW_H = 12           # an ask owns what happens in its thread for this long, or until the next ask there
LATE_ANSWER_MIN = 15        # a plain answer later than this is late
LATE_DONE_MIN = 120         # work confirmed later than this is late
OPEN_AFTER_MIN = 120        # work still unconfirmed after this long is open, not done
COMPLAINT_WINDOW_H = 24
SAME_JOB_H = 2
STREAK = 3                  # runs in a row a standing problem is seen before the review acts on it by itself
REACT_DAYS = 7              # the same problem is not acted on twice inside this window
MAX_FILED_PER_RUN = 3
CAUSE_MIN = 3               # a cause this common in a week is a standing problem
PRIORITY_KEEP_DAYS = 30
SCORE = {"recurring": 100, "repeated": 80, "not done": 60, "no answer": 60, "cause": 50, "slipped": 40, "loop": 30}
# causes only an owner can clear: graded and counted, never filed as a code fix
OWNER_CAUSES = ("sign-in", "left to the owner")

_ACK = re.compile(r"^(?:ok(?:ay)?|k|thanks?(?: you)?|thank you|thx|ty|great|good(?: work| job| one)?|perfect|nice|cool|awesome|love it|"
                  r"got it|yes|yep|yeah|yup|no|nope|sure|lol|haha+|done|fine)(?:[\s.!,]+(?:thanks?|thank you|good work|great))?[\s.!,]*$", re.I)
_PLACEHOLDER = re.compile(r"^(?:on it\b|still on it\b|working on (?:it|that)\b|one (?:sec|second|minute)\b|give me a (?:sec|second|minute)\b|"
                          r"looking into (?:it|that)\b)", re.I)
_COMPLAINT = re.compile(r"\b(?:again|still (?:not|no|waiting|broken)|didn't|did not|doesn't work|never (?:got|came|sent|happened|did)|"
                        r"where is|where's|nothing happened|not working|not what i (?:asked|said|meant)|that's wrong|wrong\b|no card|"
                        r"you (?:told|said|promised)|stuck in a loop|try again|you're supposed to|why (?:didn't|haven't|isn't)|i already)\b", re.I)

# tests replace these: (title, body, issue) -> build key, and () -> the builder's open requests
FILE_HOOK = None
OPEN_HOOK = None


# ------------------------------------------------------------------ small things
def _apos(s) -> str:
    return str(s or "").replace(chr(0x2019), "'").replace(chr(0x2018), "'")


def _ts(s) -> float:
    t = str(s or "").replace(" ", "T")[:19]
    for fmt, n in (("%Y-%m-%dT%H:%M:%S", 19), ("%Y-%m-%dT%H:%M", 16)):
        try:
            return time.mktime(time.strptime(t[:n], fmt))
        except (TypeError, ValueError):
            continue
    return 0.0


def _short(s, n: int = 140) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _title(s, n: int = 60) -> str:
    """A request's title is also its file name: plain letters, digits and spaces only, cut at a word (an ellipsis or
    a phone's curly apostrophe in it and the wiki linter refuses the note, 2026-11-15)."""
    s = re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9 ]+", " ", _URL.sub(" ", _apos(s)).replace("'", ""))).strip()
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0]


_URL = re.compile(r"https?://([^/\s\"')]+)\S*")
_LONG_ID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f-]{12,}\b|\b[A-Za-z]{0,6}\d{6,}\b|\b[0-9a-f]{16,}\b", re.I)


def clean(s) -> str:
    """Words fit to file: a link is its site's name, a long id or number is left out."""
    return _LONG_ID.sub("(a number)", _URL.sub(r"\1", str(s or "")))


def _id(text: str) -> str:
    return hashlib.sha1(re.sub(r"[^a-z0-9]+", " ", _apos(text).lower()).strip().encode("utf-8")).hexdigest()[:10]


def _owner_names() -> set:
    out = set()
    for k, u in core.owners().items():
        out.add(str(k).lower())
        first = str(u.get("name") or "").split(" ")[0].lower()
        if first:
            out.add(first)
    return out


def _people_names() -> set:
    out = set()
    for k, u in (core.people().get("users") or {}).items():
        out.add(str(k).lower())
        first = str(u.get("name") or "").split(" ")[0].lower()
        if first:
            out.add(first)
    return out


def _classify(detail: str, kind: str = "") -> str:
    try:
        import scorecard
        return scorecard._classify(detail, kind)
    except Exception:  # noqa: BLE001
        return "other"


def _quiet(detail: str) -> bool:
    try:
        import scorecard
        return scorecard.quiet_close(detail)
    except Exception:  # noqa: BLE001
        return str(detail or "").lower().startswith("closed quietly")


def _outcome_rows() -> list:
    try:
        import outcomes
        return list(outcomes.fold().values())
    except Exception:  # noqa: BLE001
        return []


def _detail(row: dict) -> str:
    return str(((row.get("checks") or [{}])[-1] or {}).get("detail") or "")


# ------------------------------------------------------------------ the grade
def asks(day: str, rows: list | None = None) -> list:
    """The asks the owners typed on `day` (YYYY-MM-DD): lines in one thread inside BURST_S are one ask; a bare
    acknowledgement and a button tap are not asks."""
    rows = rows if rows is not None else core.rows(core.TRANSCRIPT)
    owners = _owner_names()
    out = []
    for r in rows:
        if str(r.get("ts") or "")[:10] != day or str(r.get("who") or "").lower() not in owners:
            continue
        if r.get("click") or str(r.get("channel") or "") in ("card", "routine", "digest", "outcome", "scorecard"):
            continue
        text = str(r.get("text") or "").strip()
        if not text or text.startswith("["):
            continue
        where = str(r.get("thread") or r.get("convo") or "")
        t = _ts(r.get("ts"))
        last = out[-1] if out else None
        if last and last["where"] == where and last["who"] == str(r.get("who")).lower() and t - last["end"] <= BURST_S:
            last["text"] += "\n" + text
            last["end"] = t
            continue
        out.append({"ts": str(r.get("ts")), "t": t, "end": t, "who": str(r.get("who")).lower(), "where": where,
                    "thread": str(r.get("thread") or ""), "convo": str(r.get("convo") or ""), "text": text})
    return [a for a in out if not (_ACK.match(_apos(a["text"])) and len(a["text"].split()) <= 6)]


def grade(day: str, rows: list | None = None, now: float | None = None) -> list:
    """Every ask of the day with its grade, how long it took and the cause when it did not land."""
    rows = rows if rows is not None else core.rows(core.TRANSCRIPT)
    now = now or time.time()
    people = _people_names()
    owners = _owner_names()
    todays = asks(day, rows)
    outs = _outcome_rows()
    corr = [c for c in core.rows(CORRECTIONS) if str(c.get("ask_ts") or "")[:10] == day]
    for i, a in enumerate(todays):
        later = [b for b in todays[i + 1:] if b["where"] == a["where"]]
        end = later[0]["t"] if later else a["t"] + ASK_WINDOW_H * 3600
        replies, promised = [], False
        for r in rows:
            who = str(r.get("who") or "").lower()
            if who in people or who in ("card", "code", "") or str(r.get("thread") or r.get("convo") or "") != a["where"]:
                continue
            t = _ts(r.get("ts"))
            if t < a["t"] or t >= end + 60:
                continue
            if _PLACEHOLDER.match(str(r.get("text") or "").strip()):
                promised = True
                continue
            replies.append(t)
        mine = [o for o in outs if a["thread"] and str(o.get("thread") or "") == a["thread"]
                and a["t"] - 90 <= _ts(o.get("created")) < end and str(o.get("kind") or "") != "said"]
        failed = [o for o in mine if o.get("state") in ("failed", "abandoned") and not _quiet(_detail(o))]
        verified = [o for o in mine if o.get("state") == "verified"]
        open_ = [o for o in mine if o.get("state") in ("claimed", "verifying", "unverified")]
        # the owner had to say it again: a correction the ledger tied to this ask, a later line in the thread that
        # complains, or the same words typed again the same day
        # (a complaint belongs to the ask just before it in its thread, never to every earlier line: a space has one
        # thread a day, so "you didn't" at 5 PM is not about the morning's question)
        again = [c for c in corr if str(c.get("ask_ts") or "")[:16] == a["ts"][:16]
                 or (len(a["text"].split()) >= 4 and core.similar(str(c.get("ask") or ""), a["text"]))]
        nxt = later[0] if later else None
        if nxt and nxt["who"] in owners and nxt["t"] - a["t"] <= COMPLAINT_WINDOW_H * 3600 and _COMPLAINT.search(_apos(nxt["text"])):
            again.append(nxt)
            nxt["followup"] = True
        for b in todays[i + 1:]:
            if b["t"] - a["t"] > COMPLAINT_WINDOW_H * 3600:
                break
            if b is not nxt and b["who"] in owners and len(a["text"].split()) >= 4 and core.similar(a["text"], b["text"]):
                again.append(b)
                b["followup"] = True
        last_ok = max((_ts(o.get("closed") or o.get("updated")) for o in verified), default=0.0)
        last_bad = max((_ts(o.get("closed") or o.get("updated")) for o in failed), default=0.0)
        minutes = None
        cause = detail = ""
        if again:
            g = "repeated"
        elif failed and last_ok <= last_bad:
            g = "not done"
        elif not replies and not mine:
            g = "no answer" if now - a["t"] > 30 * 60 else "open"
        elif open_ and now - a["t"] > OPEN_AFTER_MIN * 60:
            g = "open"
        else:
            done_at = last_ok or (replies[0] if replies else 0.0)
            minutes = round(max(0.0, done_at - a["t"]) / 60, 1) if done_at else None
            limit = LATE_DONE_MIN if (verified or promised) else LATE_ANSWER_MIN
            g = "late" if minutes is not None and minutes > limit else "done"
        if failed:
            worst = max(failed, key=lambda o: _ts(o.get("closed") or o.get("updated")))
            detail = _short(_detail(worst), 260)
            cause = _classify(detail, str(worst.get("kind") or ""))
        elif g == "no answer":
            cause = "no answer"
        a.update(grade=g, minutes=minutes, cause=cause, detail=detail, promised=promised,
                 outcomes=len(mine), again=len(again))
    # one job that fails four times in an afternoon is one miss, not four: an ask that follows a miss in the same
    # thread inside SAME_JOB_H carries on the same job
    for i, a in enumerate(todays):
        prev = next((b for b in reversed(todays[:i]) if b["where"] == a["where"]), None)
        if prev and a["grade"] in ("not done", "repeated", "no answer") and prev["grade"] in ("not done", "repeated", "no answer") \
                and a["t"] - prev["t"] <= SAME_JOB_H * 3600:
            a["followup"] = True
    return todays


def counts(graded: list) -> dict:
    c = {"asks": len(graded)}
    for a in graded:
        c[a["grade"]] = c.get(a["grade"], 0) + 1
    return c


def causes(days: int = 7, now: float | None = None) -> dict:
    """This week's failures by cause: {cause: {"n", "example"}}; quiet closes are not failures."""
    cut = (now or time.time()) - days * 86400
    out = {}
    for o in _outcome_rows():
        if o.get("state") not in ("failed", "abandoned") or _ts(o.get("updated") or o.get("created")) < cut:
            continue
        d = _detail(o)
        if _quiet(d):
            continue
        c = _classify(d, str(o.get("kind") or ""))
        row = out.setdefault(c, {"n": 0, "example": ""})
        row["n"] += 1
        if not row["example"] or (len(d) > len(row["example"]) and len(row["example"]) < 60):
            row["example"] = _short(f"{o.get('what')}: {d}", 300)
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["n"]))


# ------------------------------------------------------------------ the build queue's ranking
def priorities() -> dict:
    d = core.load(PRIORITY, {})
    return d if isinstance(d, dict) else {}


def priority_of(key: str) -> int:
    try:
        return int((priorities().get(str(key)) or {}).get("score") or 0)
    except (TypeError, ValueError):
        return 0


def prioritize(key: str, score: int, why: str = "") -> int:
    """Rank a build request ahead of the queue. The highest score a request was ever given stands; a row older than
    PRIORITY_KEEP_DAYS is forgotten. -> the score it now holds (0 when there is no key)."""
    key = str(key or "")
    if not key:
        return 0
    d = priorities()
    cut = time.time() - PRIORITY_KEEP_DAYS * 86400
    d = {k: v for k, v in d.items() if isinstance(v, dict) and float(v.get("t") or 0) >= cut}
    cur = d.get(key) or {}
    if int(cur.get("score") or 0) < int(score):
        d[key] = {"score": int(score), "why": _short(why, 200), "ts": core.stamp(), "t": time.time()}
        core.save(PRIORITY, d)
    return int((d.get(key) or {}).get("score") or 0)


def _open_requests() -> list:
    if OPEN_HOOK:
        return OPEN_HOOK()
    try:
        import builder
        return builder.collect()
    except Exception:  # noqa: BLE001
        return []


def file_build(title: str, body: str, issue: str) -> str:
    """ONE build request for one problem, with the evidence: an open request that already covers it is returned
    instead of a second one. -> its key, '' when it could not be filed."""
    if FILE_HOOK:
        return FILE_HOOK(title, body, issue)
    marker = f"dayreview:{issue}"
    try:
        for r in _open_requests():
            if marker in str(r.get("text") or "") or core.similar(str(r.get("title") or ""), title):
                return str(r.get("key") or "")
        import wiki_writer
        tail = ("\n\n**Acceptance:** the same ask, tried again, lands and is confirmed from the record; a test encodes the "
                "path that failed. A rule or a reworded prompt is not a fix.\n\n<!-- " + marker + " -->\n")
        head = "## Build request\n\n**Ask (the daily review, by code, " + core.stamp() + "):** " + clean(title) + "\n\n"
        x = {}
        # the wiki refuses a note that looks like it holds a secret, and an owner's ask often carries a pasted link or a
        # reference number (2026-11-15: two of a day's three worst asks were refused and silently never filed). The
        # quoted words are cleaned first; if the note is still refused it is filed without the quote.
        for b in (clean(body), "The ask and what the record said are in the outcome ledger for that day; they could not be quoted here."):
            x = wiki_writer.write({"title": clean(title)[:90], "type": "workflow", "lane": "personal", "body": head + b + tail}, by="dayreview")
            if x.get("status") in ("applied", "appended"):
                return str(x.get("path") or "")
        print(f"dayreview: build request refused ({x.get('reason')}): {title}", file=sys.stderr)
        return ""
    except Exception as e:  # noqa: BLE001
        print(f"dayreview: build request not filed ({type(e).__name__}: {str(e)[:120]})", file=sys.stderr)
        return ""


# ------------------------------------------------------------------ the run
def _state() -> dict:
    d = core.load(STATE, {})
    if not isinstance(d, dict):
        d = {}
    d.setdefault("issues", {})
    d.setdefault("filed", {})
    return d


def _standing(graded: list, cz: dict) -> dict:
    """The standing problems visible today: a common cause code can fix, a loop nobody acts on."""
    out = {}
    for c, row in cz.items():
        if row["n"] >= CAUSE_MIN and c not in OWNER_CAUSES and c != "never started":
            out[f"cause:{c}"] = {"what": f"{row['n']} things this week failed for the same reason ({c})", "example": row["example"], "score": SCORE["cause"]}
    try:
        import scorecard
        for src, r in (scorecard._loops(time.time() - 7 * 86400) or {}).items():
            if r.get("verdict") == "IGNORED":
                out[f"loop:{src}"] = {"what": f"the {src} loop posted {r['generated']} cards this week and {r['acted']} were acted on",
                                      "example": "", "score": SCORE["loop"]}
    except Exception:  # noqa: BLE001
        pass
    return out


def run(day: str | None = None, dry: bool = False, now: float | None = None) -> dict:
    now = now or time.time()
    day = day or (datetime.fromtimestamp(now) - timedelta(days=1)).strftime("%Y-%m-%d")
    graded = grade(day, now=now)
    cz = causes(7, now=now)
    st = _state()
    acted = []
    filed = 0

    def _recent(issue: str) -> bool:
        return now - float((st["filed"].get(issue) or {}).get("t") or 0) < REACT_DAYS * 86400

    # 1. the day's worst three asks become code fixes
    rank = {"repeated": 0, "not done": 1, "no answer": 2}
    worst = sorted([a for a in graded if a["grade"] in rank and not a.get("followup") and a.get("cause") not in OWNER_CAUSES],
                   key=lambda a: (rank[a["grade"]], -a.get("again", 0), a["t"]))[:3]
    for a in worst:
        issue = "ask:" + _id(a["text"])
        if _recent(issue) or filed >= MAX_FILED_PER_RUN:
            continue
        why = {"repeated": "the owner had to say it again", "not done": "it did not land", "no answer": "nothing came back"}[a["grade"]]
        title = "Review fix " + _title(a["text"])
        body = (f"On {day} an owner asked this and {why}.\n\n> " + _short(a["text"], 500)
                + (f"\n\nWhat the record says went wrong ({a['cause']}): {a['detail']}" if a.get("detail") else "")
                + "\n\nFind why the ask did not land the first time and change the code so the same ask lands. "
                  "If the cause is a step only an owner can take, make the first reply say so in one line instead of trying.")
        if dry:
            acted.append({"issue": issue, "did": "would file", "what": title})
            continue
        key = file_build(title, body, issue)
        if key:
            prioritize(key, SCORE[a["grade"]], f"{day}: {why}")
            st["filed"][issue] = {"t": now, "build": key, "what": title}
            acted.append({"issue": issue, "did": "filed and ranked first", "build": key, "what": title})
            filed += 1

    # 2. a fix the review asked for that nobody started is moved up, never counted and forgotten
    slipped = 0
    open_keys = {str(r.get("key")) for r in _open_requests()}
    for o in _outcome_rows():
        if o.get("kind") == "build" and o.get("state") == "failed" and _classify(_detail(o)) == "never started" \
                and str(o.get("ref") or "") in open_keys and now - _ts(o.get("created")) < 21 * 86400:
            if not dry and prioritize(str(o["ref"]), SCORE["slipped"], "asked for by the review and never started") == SCORE["slipped"]:
                slipped += 1
    if slipped:
        acted.append({"issue": "slipped", "did": f"moved {slipped} unstarted fix(es) up the build queue"})

    # 3. a standing problem seen STREAK runs in a row is acted on, not noted again
    seen = _standing(graded, cz)
    issues = st["issues"]
    for k in list(issues):
        if k not in seen:
            issues[k]["missed"] = int(issues[k].get("missed") or 0) + 1
            if issues[k]["missed"] >= STREAK:
                issues.pop(k)
    for k, v in seen.items():
        cur = issues.setdefault(k, {"seen": 0, "first": core.stamp()})
        if str(cur.get("day") or "") != day:
            cur["seen"] = int(cur.get("seen") or 0) + 1
            cur["day"] = day
        cur.update(what=v["what"], missed=0)
        if cur["seen"] < STREAK or _recent(k) or filed >= MAX_FILED_PER_RUN:
            continue
        title = "Review fix " + _title(v["what"])
        body = (f"The daily review has seen this {cur['seen']} runs in a row (first {cur.get('first')}): {v['what']}."
                + (f"\n\nAn example from the record:\n\n> {v['example']}" if v.get("example") else "")
                + "\n\nFind the shared cause and fix it in code. For a loop nobody acts on: make what it posts worth acting on, "
                  "or stop it posting; say which you did.")
        if dry:
            acted.append({"issue": k, "did": "would file", "what": title})
            continue
        key = file_build(title, body, k)
        if key:
            prioritize(key, v["score"], f"seen {cur['seen']} runs in a row")
            st["filed"][k] = {"t": now, "build": key, "what": title}
            acted.append({"issue": k, "did": "filed and ranked", "build": key, "what": title})
            filed += 1

    cut = now - 60 * 86400
    st["filed"] = {k: v for k, v in st["filed"].items() if float(v.get("t") or 0) >= cut}
    rec = {"ts": core.stamp(), "day": day, "counts": counts(graded),
           "misses": [{"grade": a["grade"], "ask": _short(a["text"], 160), "cause": a.get("cause", ""), "detail": _short(a.get("detail"), 200)}
                      for a in graded if a["grade"] in ("repeated", "not done", "no answer", "open", "late")][:20],
           "causes": {c: r["n"] for c, r in cz.items()}, "standing": {k: v.get("seen") for k, v in issues.items()}, "acted": acted}
    if not dry:
        core.save(STATE, st)
        core.append(STORE, rec)
        try:
            import worklog
            worklog.add("Graded yesterday's asks: " + line(rec), by="dayreview", kind="review")
        except Exception:  # noqa: BLE001
            pass
    return rec


def line(rec: dict) -> str:
    c = rec.get("counts") or {}
    bits = [f"{c.get('asks', 0)} asks", f"{c.get('done', 0)} done"]
    for g, word in (("late", "late"), ("open", "still open"), ("not done", "not done"), ("no answer", "unanswered"), ("repeated", "said twice")):
        if c.get(g):
            bits.append(f"{c[g]} {word}")
    n = len([a for a in rec.get("acted") or [] if a.get("build")])
    return ", ".join(bits) + (f"; {n} fix{'es' if n != 1 else ''} started" if n else "")


def latest() -> dict:
    rows = core.rows(STORE)
    return rows[-1] if rows else {}


def dream_rows() -> list:
    """What the nightly review reads: the grade, each miss with its cause, and what this module already did about it."""
    rec = latest()
    if not rec:
        return []
    out = [f"DAYREVIEW {rec.get('day')}: {line(rec)}"]
    for m in (rec.get("misses") or [])[:8]:
        out.append(f"DAYREVIEW {m['grade']}: \"{m['ask']}\"" + (f" (cause, {m['cause']}: {m['detail']})" if m.get("detail") else ""))
    for a in (rec.get("acted") or [])[:6]:
        out.append(f"DAYREVIEW handled by code, do not file it again: {a.get('did')}" + (f": {a.get('what')}" if a.get("what") else ""))
    return out


def status() -> str:
    rec = latest()
    if not rec:
        return "dayreview: no run yet"
    out = [f"dayreview {rec.get('day')} (run {rec.get('ts')}): {line(rec)}"]
    for m in rec.get("misses") or []:
        out.append(f"- {m['grade']}: {m['ask']}" + (f" [{m['cause']}]" if m.get("cause") else ""))
    for c, n in list((rec.get("causes") or {}).items())[:6]:
        out.append(f"  cause this week: {c} x{n}")
    for k, n in (rec.get("standing") or {}).items():
        out.append(f"  standing: {k} seen {n} run(s) in a row")
    for a in rec.get("acted") or []:
        out.append(f"  acted: {a.get('did')}" + (f": {a.get('what')}" if a.get("what") else ""))
    top = sorted(priorities().items(), key=lambda kv: -int(kv[1].get("score") or 0))[:8]
    for k, v in top:
        out.append(f"  build queue first: [{v.get('score')}] {k} ({v.get('why')})")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "status", "grade", "priorities"])
    ap.add_argument("--day", default="")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.cmd == "run":
        rec = run(a.day or None, dry=a.dry_run)
        c = rec["counts"]
        print(status() if not a.dry_run else json.dumps(rec, ensure_ascii=False, indent=1))
        print(f"RESULT DAYREVIEW day={rec['day']} asks={c.get('asks', 0)} done={c.get('done', 0)} "
              f"missed={sum(c.get(g, 0) for g in ('not done', 'no answer', 'repeated'))} acted={len(rec['acted'])}")
    elif a.cmd == "grade":
        day = a.day or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        for x in grade(day):
            print(f"{x['ts'][11:16]} {x['grade']:<10} {('%.0f min' % x['minutes']) if x.get('minutes') is not None else '':<8} "
                  f"{_short(x['text'], 90)}" + (f"  [{x['cause']}]" if x.get("cause") else ""))
    elif a.cmd == "priorities":
        print(json.dumps(priorities(), ensure_ascii=False, indent=1))
    else:
        print(status())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
