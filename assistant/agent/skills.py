"""skills.py - the agent's own how-to library, written and refined by the agent as it learns.

the owner 2026-10-09: Sage should be able to take on odd jobs over time (updating a DMV document,
renting lenses from Lensrentals) and "be fully prepared to learn those skills", never saying
"I can't do that from here". A skill here is a short markdown how-to: when it applies, how we
do it, what to watch out for, and what happened the last times. The brain lists them in its
context and reads one before acting on a matching ask; the doer gets the matching skill's
steps appended to its task text, so what was learned on a site is carried to the next visit.

  assistant/agent/skills/<slug>.md      tracked in git, written only through write()

Rails: a skill is procedure, never licence: the body goes through the same refusals as memory
(no permissions to send, pay or skip a card; no secrets, codes or account numbers). The
protected core (cards, sends, money, credentials, footage) stays where it is; a skill can
only describe how to reach the point where a card is needed.
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

DIR = core.AGENT / "skills"
if core.STATE != core.AGENT / "state":                     # tests scope state/: keep their skills out of the live folder
    DIR = core.STATE.parent / "skills"
VIAS = ("do", "task", "answer")
MAX_BODY = 6000
_SECRET = re.compile(r"\b(?:password|passcode|pin|cvv|ssn|social security|routing|api key|secret key)\b\s*[:=]|\b\d{12,}\b", re.I)


def slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(title or "").lower()).strip("-")
    return s[:60] or "skill"


def _parse(text: str) -> dict:
    m = re.match(r"(?s)^---\n(.*?)\n---\n(.*)$", text)
    meta, body = {}, text
    if m:
        body = m.group(2)
        for ln in m.group(1).splitlines():
            if ":" in ln:
                k, v = ln.split(":", 1)
                meta[k.strip()] = v.strip()
    trig = meta.get("triggers", "")
    meta["triggers"] = [t.strip() for t in re.sub(r"^\[|\]$", "", trig).split(",") if t.strip()]
    return {"meta": meta, "body": body}


def all_skills() -> list:
    out = []
    if not DIR.exists():
        return out
    for p in sorted(DIR.glob("*.md")):
        try:
            d = _parse(p.read_text(encoding="utf-8"))
        except OSError:
            continue
        d["path"], d["slug"] = p, p.stem
        d["title"] = d["meta"].get("title") or p.stem
        out.append(d)
    return out


def match(text: str) -> dict | None:
    """The skill whose triggers best fit `text` (longest trigger wins), else None."""
    t = str(text or "").lower()
    best, best_len = None, 0
    for s in all_skills():
        for trig in s["meta"].get("triggers") or []:
            tl = trig.lower()
            if tl and tl in t and len(tl) > best_len:
                best, best_len = s, len(tl)
    return best


def steps_block(skill: dict, cap: int = 2500) -> str:
    """What the doer gets: the how-to without the history."""
    body = re.split(r"(?m)^## History\s*$", skill["body"])[0].strip()
    return f"HOW WE DID THIS BEFORE (my skill '{skill['title']}', follow it unless the site has changed):\n{body}"[:cap]


def write(skill: dict, by: str = "") -> dict:
    """Create or extend a skill. -> {"status": applied|appended|refused, "path", "reason"}.
    A new body for an existing title is added as a dated section; the triggers merge."""
    title = re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|#^\[\]]+', " ", str(skill.get("title") or ""))).strip()[:80]
    body = str(skill.get("body") or "").strip()[:MAX_BODY]
    via = str(skill.get("via") or "do").lower()
    lane = str(skill.get("lane") or "personal").lower()
    triggers = [re.sub(r"\s+", " ", str(t)).strip().lower()[:40] for t in (skill.get("triggers") or []) if str(t).strip()][:8]
    if len(title) < 3 or len(body) < 30:
        return {"status": "refused", "reason": "a skill needs a title and a real how-to"}
    if via not in VIAS:
        via = "do"
    if _SECRET.search(body):
        return {"status": "refused", "reason": "a skill is a procedure, never a secret"}
    if core._MEM_REFUSE.search(body):
        return {"status": "refused", "reason": "a skill is a procedure, never a permission or a secret"}
    DIR.mkdir(parents=True, exist_ok=True)
    path = DIR / f"{slugify(title)}.md"
    today = time.strftime("%Y-%m-%d")
    name = core.profile().get("name", "agent")
    if path.exists():
        old = _parse(path.read_text(encoding="utf-8"))
        merged = list(dict.fromkeys((old["meta"].get("triggers") or []) + triggers))
        head = _front(old["meta"].get("title") or title, merged, old["meta"].get("lane") or lane,
                      old["meta"].get("via") or via, old["meta"].get("created") or today, today,
                      int(old["meta"].get("uses") or 0))
        text = head + old["body"].rstrip("\n") + f"\n\n## Learned {today}" + (f" (from {by})" if by else "") + f"\n\n{body}\n"
        path.write_text(text, encoding="utf-8")
        status = "appended"
    else:
        head = _front(title, triggers, lane, via, today, today, 0)
        text = head + f"# {title}\n\n> [!note] A skill {name} taught itself on {today}" + (f" from {by}" if by else "") + ". It improves with each use.\n\n" + body + "\n"
        path.write_text(text, encoding="utf-8")
        status = "applied"
    rel = path.relative_to(core.VAULT).as_posix() if core.VAULT in path.parents else str(path)
    try:
        import wiki_writer
        wiki_writer._git("add", "--", rel)
        wiki_writer._git("commit", "-q", "-m", f"agent: skill '{title}' ({status}, from {by or 'chat'})", "--", rel)
    except Exception:  # noqa: BLE001
        pass
    cid = core.log_change("skill", f"skill {rel} ({status})", {"op": "none"}, by)
    return {"status": status, "path": rel, "change": cid}


def _front(title, triggers, lane, via, created, updated, uses) -> str:
    return ("---\n" f"title: {title}\n" f"triggers: [{', '.join(triggers)}]\n" f"lane: {lane}\n" f"via: {via}\n"
            f"created: {created}\n" f"updated: {updated}\n" f"uses: {uses}\n" "---\n\n")


def note_use(skill: dict, outcome: str = "") -> None:
    """Bump the use count and, when given, add one history line (the doer's report)."""
    try:
        p = skill["path"]
        d = _parse(p.read_text(encoding="utf-8"))
        meta = d["meta"]
        head = _front(meta.get("title") or skill["title"], meta.get("triggers") or [], meta.get("lane") or "personal",
                      meta.get("via") or "do", meta.get("created") or time.strftime("%Y-%m-%d"), time.strftime("%Y-%m-%d"),
                      int(meta.get("uses") or 0) + 1)
        body = d["body"].rstrip("\n")
        if outcome:
            if not re.search(r"(?m)^## History\s*$", body):
                body += "\n\n## History\n"
            body += f"\n- {time.strftime('%Y-%m-%d')}: {re.sub(chr(10), ' ', outcome)[:300]}"
        p.write_text(head + body + "\n", encoding="utf-8")
    except (OSError, KeyError):
        pass


def render() -> str:
    sk = all_skills()
    if not sk:
        return ""
    rel = DIR.relative_to(core.VAULT).as_posix() if core.VAULT in DIR.parents else str(DIR)
    lines = [f"SKILLS YOU HAVE TAUGHT YOURSELF (in {rel}/; Read one before acting on a matching ask; add or refine one with `skill`):"]
    for s in sk:
        m = s["meta"]
        lines.append(f"- {s['title']} ({m.get('via', 'do')}; triggers: {', '.join(m.get('triggers') or []) or '-'}; used {m.get('uses', 0)}x) -> {rel}/{s['slug']}.md")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(render() or "no skills yet")
