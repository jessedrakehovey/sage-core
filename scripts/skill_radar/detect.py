#!/usr/bin/env python3
"""detect.py — Skill Radar: the deterministic, token-free PREFILTER that finds
work the owner did by hand that (a) no installed skill covers and (b) recurs across
2+ distinct days — i.e. a candidate worth turning into a skill/workflow.

This is the cheap half. It does NOT decide whether a candidate deserves a skill;
that judgement is the `dreaming` Phase 2 LLM pass, which reads this script's
output (`candidates.json`), vets each cluster, and files vetted `new skill:`
proposals into Memory/skill-suggestions.md (gated — a new skill is a behavior
change, never auto-built). morning-ops then surfaces those for one-tap approval.

Pipeline:
    session_capture.py (SessionEnd, 0 tokens)  ->  Memory/Daily_Logs/*.md
    detect.py          (this, 0 tokens)        ->  candidates.json
    dreaming Phase 2   (1 batched LLM pass)     ->  skill-suggestions.md (Pending)
    morning-ops        (interactive, gated)     ->  approve -> build skill

Why a batch detector and not a per-session hook: a single completed task is not
evidence a skill is warranted — most tasks are one-offs. Only repetition across
days distinguishes "worth scripting" from "did it once". And one daily pass costs
nothing vs an LLM call on every SessionEnd (the usage drain that paused
automation 2026-06-23).

How "covered" is decided: each installed skill's name+description is tokenized
into a term set; a session is COVERED if its significant terms overlap an existing
skill's terms past a threshold (or the prompt names a skill outright). Uncovered
sessions are greedily clustered by term-set similarity; clusters spanning 2+ days
become candidates. Dismissed keys (dismissed.json) are latched out so a rejected
idea never re-surfaces.

Output is written to scripts/skill_radar/candidates.json. `--print` also emits a
human summary. Read-only except for its own candidates.json; never touches the
logs, the vault, or any skill.
"""

import io
import os
import re
import sys
import json
import math
import hashlib
import datetime
from pathlib import Path
from collections import Counter

VAULT = Path(__file__).resolve().parents[2]
LOGS = VAULT / "Memory" / "Daily_Logs"
HERE = Path(__file__).resolve().parent
OUT = HERE / "candidates.json"
DISMISSED = HERE / "dismissed.json"

PROJECT_SKILLS = VAULT / ".claude" / "skills"
USER_SKILLS = Path.home() / ".claude" / "skills"

# --- tuning knobs (deliberately conservative; the LLM pass is the real filter) ---
WINDOW_DAYS = 21          # how far back to scan logs
MIN_TERMS = 3             # a session needs >=3 significant terms to be judgeable
COVER_THRESHOLD = 0.50    # >=50% of a session's terms found in some skill -> covered
SIM_THRESHOLD = 0.34      # Jaccard to attach a session to an existing cluster
MIN_DAYS = 2              # a candidate must recur across >=2 distinct days
MAX_SAMPLES = 3           # representative prompts kept per candidate
DISMISS_EXPIRY_DAYS = 120 # a Skip suppresses a candidate for ~4 months, then re-offers
                          # (roast/Autonomy: a bad-timing idea in June may be right by Oct).

# Generic English + agent/meta noise that should never drive clustering.
STOP = set("""
a an the of to and or for in on at by with from into over under off out up down
is are was were be been being am do does did done doing have has had having
this that these those it its their his her our your my we you he she they them
i me us as so if then than but not no nor can could should would will shall may
might must just only also even still yet about above after again all any because
before below between both each few more most other some such too very what which
who whom whose when where why how here there now per via vs etc eg ie ok okay
get got go going gone make made making want need please thanks thank lets let
use using used run running ran see saw look looking new add added set put give
take took one two three first next last same own able like want know think
""".split())

# Things that signal *meta / dev* work, not an operational couple/vendor task.
# Kept light: the LLM still gets to judge, but these stop obvious agent-dev noise.
META = set("""
claude skill skills workflow workflows script scripts code build built building
commit committed git push pull repo file files folder path python powershell
function bug fix fixed debug test tested roast loop cron hook memory dreaming
""".split())

WORD = re.compile(r"[a-z][a-z0-9\-]{1,}")

# Light PII scrub for the human-readable `sample_prompts` written to candidates.json.
# candidates.json is gitignored + vault-local, and the dreaming pass already reads the
# raw daily logs (a strictly wider exposure), so this is hygiene, not a security control.
# Couple names aren't scrubbed (no reliable list) — noted in the README.
_EMAIL = re.compile(r"\b[\w.\-]+@[\w.\-]+\.\w+\b")
_PHONE = re.compile(r"(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}")
_MONEY = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?")


def scrub(text):
    text = _EMAIL.sub("[email]", text)
    text = _PHONE.sub("[phone]", text)
    text = _MONEY.sub("[$]", text)
    return text


# The daily logs capture EVERY session, including headless cron/executor sub-runs
# whose first "prompt" is actually an injected agent system prompt (e.g. the
# email-send executor, the voice/chat triage agents) or a scheduled-task wrapper.
# Those are already automated — they are not the owner doing unscripted work, so a
# session whose opening prompt matches any of these is dropped wholesale.
HEADLESS_OPENERS = (
    "you are ",          # agent role prompt ("You are sending ONE pre-approved...")
    "this is an automated run",
    "<scheduled-task",
    "<system-reminder",
    # nightly scout prompts open with a markdown title, not "You are" — without these
    # the radar staged the optimizer's own PROMPT.md as "recurring unscripted work"
    # (false positive sr-91aa84fc, re-detected every night until dreaming rejected it)
    "# nightly optimization scout",
    "# daily mobile ux scout",
)
HEADLESS_MARKERS = ("<scheduled-task", "do exactly this and nothing else")


def is_headless(prompts):
    """True if a session is a headless agent/cron sub-run, not the owner interacting."""
    first = prompts[0].lower().lstrip().lstrip("`'\"")
    if first.startswith(HEADLESS_OPENERS):
        return True
    blob = " ".join(prompts[:2]).lower()
    return any(mk in blob for mk in HEADLESS_MARKERS)


def significant(text):
    """Lowercase significant terms from a chunk of prompt text."""
    out = []
    for tok in WORD.findall(text.lower()):
        tok = tok.strip("-")
        if len(tok) < 3:
            continue
        if tok in STOP or tok in META:
            continue
        out.append(tok)
    return out


def load_dismissed():
    if not DISMISSED.exists():
        return {}
    try:
        data = json.loads(DISMISSED.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(data, list):  # tolerate a bare list of keys
        return {k: {} for k in data}
    if isinstance(data, dict):
        return data.get("dismissed", data) if "dismissed" in data else data
    return {}


def active_dismissals(dismissed):
    """Keys whose dismissal is still in force. A dated dismissal expires after
    DISMISS_EXPIRY_DAYS so a once-rejected idea can re-surface if it's become
    relevant; an undated one (legacy / hand-added) is treated as permanent."""
    out, today = set(), datetime.date.today()
    for k, meta in dismissed.items():
        d = (meta or {}).get("date")
        if not d:
            out.add(k)
            continue
        try:
            when = datetime.date.fromisoformat(d)
        except Exception:
            out.add(k)
            continue
        if (today - when).days < DISMISS_EXPIRY_DAYS:
            out.add(k)
    return out


def load_skills():
    """Return list of {name, terms:set} for every installed skill (both scopes)."""
    skills = []
    for root in (PROJECT_SKILLS, USER_SKILLS):
        if not root.exists():
            continue
        for sk in sorted(root.iterdir()):
            md = sk / "SKILL.md"
            if not md.is_file():
                continue
            try:
                raw = md.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            name = sk.name
            desc = ""
            m = re.search(r"(?ms)^---\s*(.*?)\s*^---", raw)
            if m:
                fm = m.group(1)
                dm = re.search(r"(?ms)^description:\s*(.*?)(?=^\w+:|\Z)", fm)
                if dm:
                    desc = dm.group(1)
            terms = set(significant(name.replace("-", " ") + " " + desc))
            skills.append({"name": name, "terms": terms})
    return skills


def parse_logs():
    """Return list of sessions: {day, sid, prompts:[raw], terms:Counter}."""
    if not LOGS.exists():
        return []
    cutoff = datetime.date.today() - datetime.timedelta(days=WINDOW_DAYS)
    sessions = []
    for f in sorted(LOGS.glob("*.md")):
        try:
            day = datetime.date.fromisoformat(f.stem)
        except ValueError:
            continue
        if day < cutoff:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        # Split into per-session blocks on the "## HH:MM — session xxxx" headers.
        blocks = re.split(r"(?m)^##\s+\d{2}:\d{2}\s+[—-]\s+session\s+", text)
        for blk in blocks[1:]:
            sid = (blk[:8] or "unknown").strip()
            prompts, closing = [], []
            mode = None
            for line in blk.splitlines():
                s = line.strip()
                if s.startswith("**Asked:**"):
                    mode = "asked"
                    continue
                if s.startswith("**Closing note"):
                    mode = "closing"
                    continue
                if line.startswith("## "):
                    break
                if mode == "asked":
                    m = re.match(r"^- (.*)$", s)
                    if m:
                        p = re.sub(r"\s*\(×\d+\)\s*$", "", m.group(1).strip())  # drop dup tag
                        if p and not p.startswith("…("):
                            prompts.append(p)
                elif mode == "closing" and s.startswith(">"):
                    closing.append(s.lstrip("> ").strip())
            if not prompts:
                continue
            if is_headless(prompts):
                continue  # cron/executor sub-run, not the owner working
            # Roast fix (Contrarian/First-Principles): score & cluster against the
            # assistant's closing note too, not just the (often terse) human prompt —
            # the closing note describes what was actually DONE, far better coverage signal.
            terms = Counter()
            for p in prompts:
                terms.update(significant(p))
            for c in closing:
                terms.update(significant(c))
            sessions.append({"day": day, "sid": sid, "prompts": prompts,
                             "closing": " ".join(closing), "terms": terms})
    return sessions


def covered(session_terms, text, skills):
    """True if some installed skill already covers this session. `text` is the
    prompts + closing note joined (the closing note often names the skill/job)."""
    st = set(session_terms)
    if not st:
        return True
    joined = text.lower()
    nospace = re.sub(r"[\s\-]", "", joined)
    for sk in skills:
        # Direct: the text literally names a skill ("do inquiry reply", "shoot prep").
        if sk["name"].replace("-", " ") in joined or sk["name"] in joined:
            return True
        if sk["name"].replace("-", "") in nospace:  # "shoot-prep" vs "shoot prep"
            return True
        overlap = len(st & sk["terms"])
        if st and overlap / len(st) >= COVER_THRESHOLD:
            return True
    return False


def jaccard(a, b):
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def cluster(uncovered):
    """Greedy single-pass clustering of uncovered sessions by term-set Jaccard."""
    clusters = []
    for s in uncovered:
        st = set(s["terms"])
        best, best_sim = None, 0.0
        for c in clusters:
            sim = jaccard(st, c["seed"])
            if sim > best_sim:
                best, best_sim = c, sim
        if best is not None and best_sim >= SIM_THRESHOLD:
            best["members"].append(s)
            best["terms"].update(s["terms"])
        else:
            clusters.append({"seed": st, "members": [s], "terms": Counter(s["terms"])})
    return clusters


def cluster_key(shared_terms):
    h = hashlib.sha1("|".join(shared_terms).encode("utf-8")).hexdigest()[:8]
    return f"sr-{h}"


def build_candidate(c, skills):
    members = c["members"]
    days = sorted({m["day"] for m in members})
    n = len(members)
    cnt = c["terms"]
    # Terms shared by at least half the members define the cluster's identity.
    half = math.ceil(n / 2)
    shared = [t for t, k in cnt.most_common() if k >= half]
    if not shared:
        shared = [t for t, _ in cnt.most_common(4)]
    shared = shared[:6]
    key = cluster_key(sorted(shared)[:4])

    # nearest existing skill (informational — helps the LLM judge "extend vs new")
    cterms = set(cnt)
    nearest, nscore = None, 0.0
    for sk in skills:
        if not cterms:
            break
        sc = len(cterms & sk["terms"]) / len(cterms)
        if sc > nscore:
            nearest, nscore = sk["name"], sc

    # representative prompts: shortest distinct ones across members
    seen, samples = set(), []
    for m in sorted(members, key=lambda m: len(" ".join(m["prompts"]))):
        for p in m["prompts"]:
            flat = " ".join(p.split())
            low = flat.lower()
            if low in seen:
                continue
            seen.add(low)
            samples.append(scrub(flat[:200]))
            break
        if len(samples) >= MAX_SAMPLES:
            break

    return {
        "key": key,
        "label": " / ".join(shared[:4]) or "(unlabeled cluster)",
        "occurrences": n,
        "days": [d.isoformat() for d in days],
        "sessions": [m["sid"] for m in members],
        "top_terms": shared,
        "sample_prompts": samples,
        "nearest_skill": {"name": nearest, "score": round(nscore, 2)} if nearest else None,
    }


def main():
    do_print = "--print" in sys.argv
    skills = load_skills()
    sessions = parse_logs()
    dismissed = active_dismissals(load_dismissed())

    uncovered = [
        s for s in sessions
        if len(set(s["terms"])) >= MIN_TERMS
        and not covered(s["terms"], " ".join(s["prompts"]) + " " + s.get("closing", ""), skills)
    ]
    clusters = cluster(uncovered)

    candidates = []
    for c in clusters:
        days = {m["day"] for m in c["members"]}
        if len(days) < MIN_DAYS:
            continue
        cand = build_candidate(c, skills)
        if cand["key"] in dismissed:
            continue  # dismissal still in force (within expiry) — don't re-surface
        candidates.append(cand)

    candidates.sort(key=lambda x: (x["occurrences"], len(x["days"])), reverse=True)

    payload = {
        "generated": datetime.date.today().isoformat(),
        "window_days": WINDOW_DAYS,
        "skills_indexed": len(skills),
        "sessions_scanned": len(sessions),
        "sessions_uncovered": len(uncovered),
        "candidate_count": len(candidates),
        "dismissed_active": len(dismissed),
        "candidates": candidates,
    }
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    if do_print:
        print(f"skill-radar: {len(sessions)} sessions / {WINDOW_DAYS}d, "
              f"{len(uncovered)} uncovered, {len(candidates)} candidate(s), "
              f"{len(dismissed)} dismissed.")
        for c in candidates:
            print(f"\n  [{c['key']}] {c['label']}  "
                  f"(x{c['occurrences']} over {len(c['days'])} days: {', '.join(c['days'])})")
            if c["nearest_skill"] and c["nearest_skill"]["score"] >= 0.2:
                print(f"      nearest skill: {c['nearest_skill']['name']} "
                      f"({c['nearest_skill']['score']})")
            for s in c["sample_prompts"]:
                print(f"      - {s[:140]}")
    else:
        print(f"skill-radar: wrote {len(candidates)} candidate(s) to {OUT.name}")


if __name__ == "__main__":
    main()
