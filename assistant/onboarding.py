"""onboarding.py - Discovery, Training, the never-list, the shadow week, go-live (sage-core, 2026-10-27).

the owner's plan had `tools`, `consultation` and `training` as three commands and a 20-question minimum. Here:
  discover   ONE conversation over the business functions (assistant/onboarding/discovery.json): what tool, and the
             catalog picks the access tier (catalog.py). "Where are your leads" and "what CRM" are the same answer.
  map        the capability map the owner can read: what the agent reads, writes, and what always waits for a tap.
  coverage   Training's stopping rule is COVERAGE, not a count: every stage of the business's lifecycle skeleton
             (assistant/onboarding/lifecycles/<type>.json) needs trigger, actor, channel, template, clock, verifier.
  train      asks the next missing field until coverage is 100%; answers land in data/onboarding.json (machine-
             readable), and `blueprint` renders them into a wiki note as a by-product, never the source.
  seed-wiki  the wiki schema's first custom vocabulary (agent/schema_growth.py): the lifecycle skeleton's `wiki` section
             (a type per core noun, tags from the stage names, a folder per type) plus the owner's own words for what the
             business tracks (discovery's `tracks` answer). Runs on its own after discover and train; idempotent.
  never      the never-list (agent/state/org.json -> never): seeded at maximum gating, loosened only by name.
  shadow     the shadow week: everything drafted and carded, nothing sent on its own (config.json shadow + switches).
  golive     the standing exceptions, switched on one by one, by name, with the date recorded.

Non-interactive: --answers <file.json> for discover ({function: "tool"}) and train ({stage: {field: value}}).
  python assistant/onboarding.py discover|map|coverage [--quiet]|train|never [--add ..]|shadow on|off|status|golive [--enable key]|blueprint
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "scripts"))
import catalog  # noqa: E402
import orgcfg  # noqa: E402

DATA = Path(os.environ["ASSISTANT_DATA"]) if os.environ.get("ASSISTANT_DATA") else HERE / "data"
STATE = DATA / "onboarding.json"
CONFIG = HERE / "config.json"
AGENT_STATE = Path(os.environ["AGENT_STATE"]) if os.environ.get("AGENT_STATE") else HERE / "agent" / "state"
ORG_LEARNED = AGENT_STATE / "org.json"
DISCOVERY = HERE / "onboarding" / "discovery.json"
LIFECYCLES = HERE / "onboarding" / "lifecycles"
FIELDS = ["trigger", "actor", "channel", "template", "clock", "verifier"]
FIELD_ASK = {
    "trigger": "What starts this stage? (an email, a form, a payment, a date, a reply, you saying so)",
    "actor": "Who does it today: you, a staff member, or should the agent do it (drafting, with your tap)?",
    "channel": "Where does it happen: email, text, the CRM, a call, in person?",
    "template": "The words you use. Paste a real recent example, or 'none' if there is no fixed wording.",
    "clock": "How fast must it happen? (within 2 hours; the same day; 14 days before; 2 days after)",
    "verifier": "How would we know it happened? (a reply, a record in the CRM, a paid invoice, a calendar event)",
}
NEVER_SEED = [
    "Nothing outward (email, text, post, message, form) goes without an owner's tap on a card.",
    "Never commit to a price, a date, a deadline or anything contractual; those are blanks for the owner.",
    "Never pay, transfer, refund or move money; never enter a credential anywhere.",
    "Never delete a file, a message or a record; never move original media.",
    "Never speak as the owner to a client about anything the owner has not already said.",
]
STANDING_SWITCHES = {
    "texts.enabled": "the standing client texts go out from the owner's phone without a card",
    "leads.auto_send_collection": "the priced collection goes to a lead on its own when the date is open",
    "cards.delivery": "weekly = cards held for one Monday digest; immediate = every card the minute it is posted",
}


def _load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def _save(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def state() -> dict:
    s = _load(STATE, {}) or {}
    s.setdefault("discovery", {})
    s.setdefault("lifecycle", {"type": str(orgcfg.get("lifecycle") or "service-studio"), "stages": {}})
    s.setdefault("golive", [])
    return s


def lifecycle(kind: str) -> dict:
    d = _load(LIFECYCLES / f"{kind}.json", {})
    if not d.get("stages"):
        d = _load(LIFECYCLES / "service-studio.json", {"stages": []})
    return d


def _ask(prompt: str, answers: dict | None, key, default: str = "") -> str:
    if answers is not None:
        v = answers
        for k in (key if isinstance(key, tuple) else (key,)):
            v = v.get(k, {}) if isinstance(v, dict) else {}
        return str(v if isinstance(v, str) else (v or default))
    try:
        return input(prompt + " ").strip() or default
    except EOFError:
        return default


# ---------------------------------------------------------------- discovery
def discover(answers: dict | None = None, only: str = "") -> dict:
    s = state()
    cat = catalog.load()
    disc = _load(DISCOVERY, {"functions": []})
    for fn in disc.get("functions", []):
        fid = fn["id"]
        if only and fid != only:
            continue
        cur = s["discovery"].get(fid, {})
        if cur.get("tool") and answers is None and not only:
            continue
        tool = _ask(f"[{fid}] {fn['ask']} (examples: {', '.join(fn.get('examples') or [])[:80]}; 'none' / 'skip')", answers, fid, "")
        if not tool:
            continue
        if tool.lower() in ("none", "skip", "n/a", "-"):
            s["discovery"][fid] = {"tool": "", "status": "skipped", "when": time.strftime("%Y-%m-%d")}
            continue
        e = catalog.match(tool, cat)
        bt = catalog.best_tier(e) if e else None
        s["discovery"][fid] = {"tool": tool, "catalog_id": e["id"] if e else "", "tier": int(bt["tier"]) if bt else 0,
                               "path": bt.get("path") if bt else "", "status": "to-connect" if e else "uncatalogued",
                               "how": bt.get("how") if bt else "No catalog entry yet: the agent asks how you reach it today and writes one.",
                               "when": time.strftime("%Y-%m-%d")}
    ask = disc.get("tracks") or {}
    if ask and not only and (answers is not None and answers.get("tracks") or answers is None and not s.get("tracks")):
        raw = answers.get("tracks") if answers is not None else _ask(f"[tracks] {ask.get('ask', '')} (examples: {', '.join(ask.get('examples') or [])})", None, "tracks", "")
        if raw:
            s["tracks"] = parse_tracks(raw)
    _save(STATE, s)
    seed_wiki(s)
    return s


def parse_tracks(raw) -> list:
    """'Jobs, customers and units' / ["jobs", "units"] -> ["jobs", "customers", "units"] (the owner's own words)."""
    import re
    items = raw if isinstance(raw, list) else re.split(r",|;|/|\band\b|\n", str(raw or ""))
    out = []
    for x in items:
        x = re.sub(r"^(?:our|the|my|all|every)\s+", "", str(x).strip().lower()).strip(" .")
        if x and len(x) <= 40 and x not in out:
            out.append(x)
    return out[:12]


def seed_wiki(s: dict | None = None, commit: bool = True) -> dict:
    """Write the schema's first custom vocabulary from the learned business type and the owner's words (schema_growth.seed:
    linted, ledgered, undoable; what the schema already has is skipped, so a re-run adds only what is new). Never fatal."""
    s = s or state()
    kind = s["lifecycle"].get("type") or "service-studio"
    try:
        sys.path.insert(0, str(HERE / "agent"))
        import schema_growth
        return schema_growth.seed(lifecycle(kind), s.get("tracks") or [], by="onboarding", commit=commit)
    except Exception as e:  # noqa: BLE001
        return {"status": "skipped", "reason": f"{type(e).__name__}: {str(e)[:160]}"}


def capability_map(s: dict | None = None) -> str:
    s = s or state()
    cat = catalog.load()
    disc = _load(DISCOVERY, {"functions": []})
    lines = ["What the agent can reach, by business function:"]
    for fn in disc.get("functions", []):
        d = s["discovery"].get(fn["id"])
        if not d:
            lines.append(f"- {fn['id']}: not asked yet")
            continue
        if not d.get("tool"):
            lines.append(f"- {fn['id']}: skipped")
            continue
        e = cat.get(d.get("catalog_id") or "")
        bt = catalog.best_tier(e) if e else None
        if not bt:
            lines.append(f"- {fn['id']}: {d['tool']} (no catalog entry: owner's portal until one is written)")
            continue
        lines.append(f"- {fn['id']}: {e['name']} via tier {bt['tier']} ({catalog.TIER_NAMES[int(bt['tier'])]}), {d.get('status')}")
        lines.append(f"    reads: {', '.join(bt.get('reads') or []) or 'nothing yet'}")
        lines.append(f"    writes: {', '.join(bt.get('writes') or []) or 'nothing'}")
        lines.append(f"    always a card: {', '.join(bt.get('always_tap') or []) or 'every outbound message, every payment, every deletion'}")
    return "\n".join(lines)


# ---------------------------------------------------------------- training
def coverage(s: dict | None = None) -> dict:
    s = s or state()
    kind = s["lifecycle"].get("type") or "service-studio"
    skel = lifecycle(kind)
    stages = skel.get("stages", [])
    filled = s["lifecycle"].get("stages", {})
    total = len(stages) * len(FIELDS)
    have = 0
    missing = {}
    for st in stages:
        got = filled.get(st["id"], {})
        miss = [f for f in FIELDS if not str(got.get(f) or "").strip()]
        have += len(FIELDS) - len(miss)
        if miss:
            missing[st["id"]] = miss
    pct = int(round(100 * have / total)) if total else 0
    return {"type": kind, "name": skel.get("name", kind), "pct": pct, "stages": len(stages), "missing": missing, "complete": not missing and bool(stages)}


def train(answers: dict | None = None, only_stage: str = "", limit: int = 0) -> dict:
    s = state()
    kind = s["lifecycle"].get("type") or "service-studio"
    skel = lifecycle(kind)
    asked = 0
    for st in skel.get("stages", []):
        if only_stage and st["id"] != only_stage:
            continue
        got = s["lifecycle"]["stages"].setdefault(st["id"], {})
        for f in FIELDS:
            if str(got.get(f) or "").strip():
                continue
            prompt = f"[{st['name']}: {st.get('what', '')}] {FIELD_ASK[f]}" + (f" (typical: {st.get('typical_' + f)})" if st.get("typical_" + f) else "")
            v = _ask(prompt, answers, (st["id"], f), "")
            if v:
                got[f] = v
                got["when"] = time.strftime("%Y-%m-%d")
            asked += 1
            if limit and asked >= limit:
                _save(STATE, s)
                return coverage(s)
    _save(STATE, s)
    seed_wiki(s)
    return coverage(s)


# ---------------------------------------------------------------- the never-list, the shadow week, go-live
def never_list(add: str = "", seed: bool = True) -> list:
    org = _load(ORG_LEARNED, {}) or {}
    nev = [x for x in (org.get("never") or []) if isinstance(x, str)]
    if seed and not nev:
        nev = list(NEVER_SEED)
    if add and add not in nev:
        nev.append(add)
    org["never"] = nev
    org["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _save(ORG_LEARNED, org)
    return nev


def _get(cfg: dict, dotted: str):
    v = cfg
    for k in dotted.split("."):
        v = v.get(k) if isinstance(v, dict) else None
    return v


def _set(cfg: dict, dotted: str, value) -> None:
    ks = dotted.split(".")
    d = cfg
    for k in ks[:-1]:
        d = d.setdefault(k, {})
    d[ks[-1]] = value


def shadow(mode: str) -> dict:
    cfg = _load(CONFIG, {}) or {}
    sh = cfg.setdefault("shadow", {})
    if mode == "on":
        sh.update({"on": True, "since": time.strftime("%Y-%m-%d")})
        if isinstance(cfg.get("texts"), dict):
            cfg["texts"]["enabled"] = False
        if isinstance(cfg.get("leads"), dict):
            cfg["leads"]["auto_send_collection"] = False
        _save(CONFIG, cfg)
    elif mode == "off":
        sh.update({"on": False, "ended": time.strftime("%Y-%m-%d")})
        _save(CONFIG, cfg)
    return {"on": bool(sh.get("on")), "since": sh.get("since", ""), "ended": sh.get("ended", "")}


def golive(enable: str = "", value: str = "") -> list:
    cfg = _load(CONFIG, {}) or {}
    s = state()
    if enable:
        if enable not in STANDING_SWITCHES:
            raise SystemExit(f"unknown switch {enable}; known: {', '.join(STANDING_SWITCHES)}")
        val = value if value else True
        if isinstance(val, str) and val.lower() in ("true", "false"):
            val = val.lower() == "true"
        _set(cfg, enable, val)
        _save(CONFIG, cfg)
        s["golive"].append({"switch": enable, "value": val, "when": time.strftime("%Y-%m-%dT%H:%M")})
        _save(STATE, s)
    rows = []
    for k, what in STANDING_SWITCHES.items():
        rows.append({"switch": k, "value": _get(cfg, k), "what": what})
    return rows


def _schema_tags() -> set:
    """The tags Schema/schema.md allows (its fenced ```tags block), so the blueprint's front matter passes lint."""
    try:
        import re
        m = re.search(r"```tags\n(.*?)```", (ROOT / "Schema" / "schema.md").read_text(encoding="utf-8"), re.S)
        return set(m.group(1).split()) if m else set()
    except OSError:
        return set()


def blueprint(s: dict | None = None) -> Path:
    s = s or state()
    cov = coverage(s)
    skel = lifecycle(cov["type"])
    org = orgcfg.get("name", "the organization")
    p = ROOT / "Wiki" / "Operations" / "Onboarding Blueprint.md"
    today = time.strftime("%Y-%m-%d")
    created = today
    if p.exists():                      # `created` never changes after creation (the schema's rule)
        import re
        m = re.search(r"^created: (\d{4}-\d{2}-\d{2})", p.read_text(encoding="utf-8", errors="replace"), re.M)
        created = m.group(1) if m else today
    allowed = _schema_tags()
    tags = [t for t in ("onboarding", "blueprint", "automation") if t in allowed][:2]
    # the full front matter Schema/schema.md requires (2026-10-29: the old title/date/status:complete header failed
    # lint, and the agent's note writer then refused every chat note in a new organization); completeness is a heading
    lines = ["---", "type: workflow", "area: operations", f"tags: [{', '.join(tags)}]", f"created: {created}", f"updated: {today}",
             "sources: [\"[[Onboarding Blueprint]]\"]", "status: draft", "---", "", f"# {org}: onboarding blueprint", "",
             "> [!info] Rendered by `assistant/onboarding.py blueprint` from `assistant/data/onboarding.json`. Edit the answers, not this note. "
             + ("Onboarding is **complete**." if cov["complete"] else "Onboarding is **in progress**."), "",
             "## Tools and access", "", capability_map(s).replace("What the agent can reach, by business function:", "").strip(), "",
             f"## Lifecycle: {cov['name']} ({cov['pct']}% covered)", ""]
    for st in skel.get("stages", []):
        got = s["lifecycle"]["stages"].get(st["id"], {})
        lines += [f"### {st['name']}", "", f"_{st.get('what', '')}_", ""]
        for f in FIELDS:
            lines.append(f"- **{f}:** {got.get(f) or '_missing_'}")
        lines.append("")
    try:                                # the folders the schema routes to, seeded from the lifecycle and grown from use
        sys.path.insert(0, str(HERE / "agent"))
        import schema_growth
        fl = [f for f in schema_growth.folders() if (ROOT / "Wiki" / f).is_dir()]
    except Exception:  # noqa: BLE001
        fl = []
    if fl:
        lines += ["## Wiki folders", "", "Where the agent files what it learns (the schema's routing; it grows as new kinds of notes are asked for).", ""]
        lines += [f"- [[Wiki/{f}/about|{f}]]" if (ROOT / "Wiki" / f / "about.md").exists() else f"- `Wiki/{f}/`" for f in fl] + [""]
    nev = never_list(seed=False)
    lines += ["## Never", ""] + [f"- {x}" for x in nev] + ["", "## Standing exceptions switched on", ""]
    lines += [f"- {g['switch']} = {g['value']} ({g['when']})" for g in s.get("golive", [])] or ["- none yet (shadow week)"]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------- cli
def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("discover"); p.add_argument("--answers", default=""); p.add_argument("--function", default="")
    sub.add_parser("map")
    p = sub.add_parser("coverage"); p.add_argument("--quiet", action="store_true"); p.add_argument("--json", action="store_true")
    p = sub.add_parser("train"); p.add_argument("--answers", default=""); p.add_argument("--stage", default=""); p.add_argument("--limit", type=int, default=0)
    p = sub.add_parser("never"); p.add_argument("--add", default="")
    p = sub.add_parser("shadow"); p.add_argument("mode", choices=["on", "off", "status"])
    p = sub.add_parser("golive"); p.add_argument("--enable", default=""); p.add_argument("--value", default="")
    sub.add_parser("blueprint")
    sub.add_parser("seed-wiki")
    a = ap.parse_args()
    if a.cmd == "discover":
        ans = _load(Path(a.answers), {}) if a.answers else None
        s = discover(ans, a.function)
        print(capability_map(s))
        print("\nYour own documents: put them in Raw/ (any type: md, pdf, docx, pptx, transcripts), then "
              "`python scripts/wiki_tool.py sources` lists what the wiki does not hold yet; say \"ingest\" in a Claude session "
              "(the wiki-ingest skill) to compile them into Wiki/ notes. Nothing is ingested on its own until the nightly dreaming job runs.")
    elif a.cmd == "map":
        print(capability_map())
    elif a.cmd == "coverage":
        c = coverage()
        if a.json:
            print(json.dumps(c, indent=1))
        elif not a.quiet:
            for st, miss in c["missing"].items():
                print(f"  {st:18} missing: {', '.join(miss)}")
        print(f"COVERAGE {c['pct']}% lifecycle={c['type']} stages={c['stages']} incomplete={len(c['missing'])}")
    elif a.cmd == "train":
        ans = _load(Path(a.answers), {}) if a.answers else None
        c = train(ans, a.stage, a.limit)
        print(f"COVERAGE {c['pct']}% lifecycle={c['type']} stages={c['stages']} incomplete={len(c['missing'])}" + ("  (training complete: run `shadow on`)" if c["complete"] else ""))
    elif a.cmd == "never":
        for x in never_list(a.add):
            print(f"- {x}")
    elif a.cmd == "shadow":
        r = shadow(a.mode)
        print(f"shadow week: {'ON since ' + r['since'] if r['on'] else 'off' + (' (ended ' + r['ended'] + ')' if r['ended'] else '')}")
    elif a.cmd == "golive":
        for r in golive(a.enable, a.value):
            print(f"  {r['switch']:28} = {json.dumps(r['value'])}   {r['what']}")
    elif a.cmd == "seed-wiki":
        r = seed_wiki()
        plan = r.get("plan") or {}
        print(f"SEED {r.get('status')} types={[t[0] for t in plan.get('types') or []]} tags={plan.get('tags') or []} "
              f"folders={r.get('folders') or []} {r.get('reason', '')}".rstrip())
    elif a.cmd == "blueprint":
        print(f"wrote {blueprint()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
