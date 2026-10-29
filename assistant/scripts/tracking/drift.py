"""drift.py - the record says one thing, the evidence says it already happened (ruling 2, 2026-10-15).

example's self_done.py + close_chase.py, generalized to four detectors, each on/off in
assistant/tracking.json -> drift:

  ledger_stage    the delivery ledger (scripts/projects/project_state.json) lags the editor:
                  an editor project exists but the ledger still says booked / pre-shoot; the
                  wedding day has passed (booked couple) but the ledger still says pre-shoot.
                  FIX: set the ledger stage (internal write).
  crm_stage       the CRM snapshot (HoneyBook) still shows a pre-event stage after the wedding
                  day, or a booked client still sitting in a lead stage. MANUAL: stage moves in
                  HoneyBook stay the owner's (README "Moving a pipeline stage stays manual").
  armed_reminder  a money/home follow-up is still waiting although a later mail ticket says it
                  was paid / delivered. FIX: close the follow-up (internal write).
  past_event      a timed calendar event is 1-4 days past and nothing about it appears in chat,
                  tickets or cards since. MANUAL: "anything to follow up?"

Read-only on every record. `findings()` is pure; `run` caches them in data/tracking/drift.json
for the weekly card and the chaser. `apply` performs the internal fixes and is reached ONLY
through an approved `tracking_fix` executor ticket (drain.py), never from here on its own.

  python assistant/scripts/tracking/drift.py            # list the findings now
  python assistant/scripts/tracking/drift.py run        # refresh the cache
  python assistant/scripts/tracking/drift.py apply --fixes-file f.json [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

LEDGER_ORDER = ["inquiry", "booked", "pre-shoot", "ingest", "edit", "review", "revisions", "delivered"]
TERMINAL = {"delivered", "cancelled", "lost", "archived"}


def _order(stage: str) -> int:
    return LEDGER_ORDER.index(stage) if stage in LEDGER_ORDER else -1


def _finding(key, kind, lane, title, text, *, why="", fix=None, manual="", age_days=0.0, amount=None, waiting_on=""):
    f = {"key": key, "kind": kind, "lane": lane, "title": title[:120], "text": text[:400], "why": why,
         "fix": fix, "manual": manual, "age_days": round(float(age_days), 1)}
    if amount is not None:
        f["amount"] = amount
    if waiting_on:
        f["waiting_on"] = waiting_on
    return f


# ------------------------------------------------------------------ 1. the delivery ledger vs the editor
def _editor_projects(root: Path) -> list:
    try:
        return [p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("_") and (p / "wedding.json").exists()]
    except OSError:
        return []


def _matches_editor(couple: str, folders: list) -> str:
    cw = {w for w in re.findall(r"[a-z]{4,}", str(couple).lower()) if w not in {"project", "wedding"}}
    for f in folders:
        fw = set(re.findall(r"[a-z]{4,}", f.lower()))
        if cw & fw:
            return f
    return ""


def ledger_stage(d: dict, now=None, ledger: dict | None = None, folders: list | None = None) -> list:
    now = now or C.now_local()
    out = []
    path = C.vault_path(d.get("ledger") or "scripts/projects/project_state.json")
    led = ledger if ledger is not None else (C.load_json(path, {}) or {})
    projects = led.get("projects") or {}
    folders = folders if folders is not None else _editor_projects(Path(d.get("projects_root") or "D:/WeddingEditor/projects"))
    grace = int(d.get("event_grace_days") or 2)
    max_past = int(d.get("max_days_past") or 120)
    lane = str(d.get("lane") or "wedding")
    for key, p in projects.items():
        stage = str(p.get("stage") or "")
        if stage in TERMINAL or not stage:
            continue
        couple = str(p.get("couple") or key)
        booked = str(p.get("honeybook_status") or "") == "client" or stage in ("booked", "pre-shoot", "ingest", "edit", "review", "revisions")
        folder = _matches_editor(couple, folders)
        if folder and _order(stage) < _order("edit"):
            out.append(_finding(f"ledger:{key}:edit", "drift", lane, f"{couple}: editing has started",
                                f"The editor has a project for {couple} ({folder}) but the delivery ledger still says '{stage}'.",
                                why=f"set the ledger to 'edit' so delivery tracking (6-8 weeks) starts counting",
                                fix={"op": "ledger_stage", "ledger": str(path), "key": key, "stage": "edit",
                                     "desc": f"{couple}: ledger stage {stage} -> edit"},
                                age_days=C.age_days(p.get("seeded") or p.get("last_action"), now)))
            continue
        wd = C.parse_any(p.get("wedding_date"))
        post = str(d.get("post_event_stage") or "ingest")
        if wd and booked and grace <= (now - wd).days <= max_past and _order(stage) < _order(post):
            out.append(_finding(f"ledger:{key}:{post}", "drift", lane, f"{couple}: the wedding was {C.shortdate(wd)}",
                                f"{couple}'s wedding was {C.shortdate(wd)} and the delivery ledger still says '{stage}'.",
                                why=f"mark it '{post}' so the footage backup and the delivery clock are tracked",
                                fix={"op": "ledger_stage", "ledger": str(path), "key": key, "stage": post,
                                     "desc": f"{couple}: ledger stage {stage} -> {post}"},
                                age_days=(now - wd).days))
    return out


# ------------------------------------------------------------------ 2. the CRM pipeline (HoneyBook snapshot)
def crm_stage(d: dict, now=None, snap: dict | None = None) -> list:
    now = now or C.now_local()
    out = []
    snap = snap if snap is not None else (C.load_json(C.vault_path(d.get("snapshot") or "dashboard/data/honeybook.json"), {}) or {})
    stages = {s.get("stage_id"): s for s in snap.get("stages") or []}
    grace = int(d.get("event_grace_days") or 2)
    max_past = int(d.get("max_days_past") or 120)
    min_order = int(d.get("post_event_min_order") or 13)
    lane = str(d.get("lane") or "wedding")
    for p in snap.get("projects") or []:
        if str(p.get("status") or "") == "archived":
            continue
        st = stages.get(p.get("stage_id")) or {}
        name, cat, order = str(st.get("name") or "?"), str(st.get("category") or ""), int(st.get("order") or 0)
        who = str(p.get("name") or "").replace("'s Project", "").strip()
        wd = C.parse_any(p.get("date"))
        booked = str(p.get("status") or "") == "client"
        # 2026-10-18 (BUILD_PLAN 1.2): a lagging HoneyBook stage is a FIX the card can apply (hb_set_stage through the
        # connector, read back), not "[HoneyBook, your hand]". Target stages come from tracking.json -> drift.crm_stage.
        booked_stage = str(d.get("booked_stage") or "Proposal Signed")
        post_stage = str(d.get("post_event_stage") or "File Management")
        if booked and cat == "lead":
            out.append(_finding(f"crm:{p.get('id')}:lead", "drift", lane, f"{who}: booked but still a lead stage",
                                f"{who} is a client in HoneyBook but the pipeline still shows '{name}' (a lead stage).",
                                why="move the project to a booked stage in HoneyBook",
                                fix={"op": "hb_stage", "key": f"crm:{p.get('id')}:lead", "workspace_id": str(p.get("id") or ""), "stage": booked_stage,
                                     "desc": f"move {who} to '{booked_stage}' in HoneyBook"},
                                age_days=C.age_days(p.get("stage_moved_at"), now)))
        elif booked and wd and grace <= (now - wd).days <= max_past and cat == "booked" and order < min_order:
            out.append(_finding(f"crm:{p.get('id')}:post", "drift", lane, f"{who}: wedding was {C.shortdate(wd)}, HoneyBook says {name}",
                                f"{who}'s wedding was {C.shortdate(wd)}; HoneyBook still shows '{name}'.",
                                why="move it along the pipeline so HoneyBook's tasks fire",
                                fix={"op": "hb_stage", "key": f"crm:{p.get('id')}:post", "workspace_id": str(p.get("id") or ""), "stage": post_stage,
                                     "desc": f"move {who} to '{post_stage}' in HoneyBook"},
                                age_days=(now - wd).days))
    return out


# ------------------------------------------------------------------ 3. reminders still armed after the thing happened
def armed_reminder(d: dict, now=None, fu_state: dict | None = None, tickets: dict | None = None) -> list:
    now = now or C.now_local()
    out = []
    st = fu_state if fu_state is not None else C.followups_state()
    tix = tickets if tickets is not None else C.tickets_all()
    settled = [w.lower() for w in (d.get("settled_words") or [])]
    sops = set(d.get("sops") or [])
    mails = [t for t in tix.values() if str(t.get("kind") or "") in ("mail", "email")]
    for it in (st.get("items") or {}).values():
        if it.get("status") != "waiting" or (sops and it.get("sop") not in sops):
            continue
        subj = C.words(str(it.get("subject") or "") + " " + str(it.get("unit") or ""))
        if not subj:
            continue
        armed = C.parse_any(it.get("created"))
        for t in mails:
            if armed and (C.parse_any(t.get("created")) or armed) < armed:
                continue
            blob = (str(t.get("title") or "") + " " + str(t.get("summary") or "")).lower()
            if any(w in blob for w in settled) and len(subj & C.words(blob)) >= 1:
                out.append(_finding(f"fu:{it['id']}", "drift", str(it.get("lane") or "money"),
                                    f"Reminder still armed: {str(it.get('subject') or it.get('unit'))[:60]}",
                                    f"'{str(t.get('title'))[:80]}' arrived {C.shortdate(t.get('created'))}, but the reminder "
                                    f"'{str(it.get('subject') or '')[:60]}' is still waiting to fire.",
                                    why="close it so you are not reminded about something already done",
                                    fix={"op": "followup_done", "id": it["id"], "desc": f"close reminder {it['id']}: {str(it.get('subject') or '')[:60]}"},
                                    age_days=C.age_days(t.get("created"), now)))
                break
    return out


# ------------------------------------------------------------------ 4. calendar events that happened
def _seen_events_update(cal: dict, seen: dict) -> dict:
    for e in cal.get("events") or []:
        uid = str(e.get("uid") or f"{e.get('date')}|{e.get('title')}")
        if uid not in seen:
            seen[uid] = {"title": e.get("title"), "date": e.get("date"), "time": e.get("time"), "owner": e.get("owner"),
                         "all_day": bool(e.get("all_day")), "booking": e.get("booking") or ""}
    cut = (C.now_local() - timedelta(days=30)).strftime("%Y-%m-%d")
    return {k: v for k, v in seen.items() if str(v.get("date") or "") >= cut}


def past_event(d: dict, now=None, cal: dict | None = None, seen: dict | None = None, mentions: list | None = None) -> list:
    now = now or C.now_local()
    cal = cal if cal is not None else (C.load_json(C.vault_path(d.get("calendar") or "assistant/data/calendar.json"), {}) or {})
    seen_in = seen if seen is not None else C.state("seen_events", {})
    seen_out = _seen_events_update(cal, dict(seen_in))
    if seen is None:
        C.save_state("seen_events", seen_out)
    lo, hi = int(d.get("min_days_past") or 1), int(d.get("max_days_past") or 4)
    skip = [w.lower() for w in (d.get("skip_title_words") or [])]
    if mentions is None:
        mentions = [str(r.get("text") or "") for r in C.transcript(hi + 1)]
        mentions += [str(t.get("title") or "") + " " + str(t.get("summary") or "") for t in C.tickets_all().values()
                     if C.age_days(t.get("created"), now) <= hi + 1]
        mentions += [str((c.get("questions") or [{}])[0].get("question") or "") for c in C.cards_live()]
    out = []
    for uid, e in seen_out.items():
        ed = C.parse_any(e.get("date"))
        if not ed or e.get("all_day") or not e.get("time"):
            continue
        days = (now.date() - ed.date()).days
        if not (lo <= days <= hi):
            continue
        title = str(e.get("title") or "")
        if any(w in title.lower() for w in skip) or e.get("booking") in ("booked", "inquiry"):
            continue                                       # weddings and leads are tracked by their own chains
        tw = C.words(title)
        if not tw:
            continue
        if any(len(tw & C.words(m)) >= min(2, len(tw)) for m in mentions):
            continue
        lane = "owner6" if str(e.get("owner") or "") == "owner6" else str(d.get("lane") or "personal")
        out.append(_finding(f"event:{uid}", "drift", lane, f"{title[:60]} ({C.shortdate(ed)})",
                            f"'{title}' was on the calendar {C.shortdate(ed)} at {e.get('time')} and nothing about it has come up since.",
                            why="anything to follow up: a thank-you, notes, a next step? Reply in the thread and I'll track it",
                            manual="ask", age_days=days))
    return out


# ------------------------------------------------------------------ all of them
def findings(now=None, c: dict | None = None) -> list:
    d = (c or C.cfg())["drift"]
    now = now or C.now_local()
    out = []
    for name, fn in (("ledger_stage", ledger_stage), ("crm_stage", crm_stage), ("armed_reminder", armed_reminder), ("past_event", past_event)):
        sub = d.get(name) or {}
        if not sub.get("enabled"):
            continue
        try:
            out.extend(fn(sub, now))
        except Exception as e:  # noqa: BLE001 - one detector failing must not hide the others
            C.log("drift_error", detector=name, error=f"{type(e).__name__}: {str(e)[:200]}")
    latch = C.state("drift_latch", {})
    out = [f for f in out if f["key"] not in latch]
    return out


def run() -> dict:
    fs = findings()
    cache = {"ts": C.now_local().strftime("%Y-%m-%d %H:%M"), "findings": fs}
    C.save_state("drift", cache)
    print(f"DRIFT findings={len(fs)} fixable={sum(1 for f in fs if f.get('fix'))}")
    return cache


def cached() -> list:
    return list((C.state("drift", {}) or {}).get("findings") or [])


def latch(keys: list, why: str = "dropped") -> None:
    lt = C.state("drift_latch", {})
    for k in keys:
        lt[k] = {"why": why, "ts": C.now_local().strftime("%Y-%m-%d")}
    C.save_state("drift_latch", lt)


# ------------------------------------------------------------------ the writes (approved ticket only)
def apply(fixes: list, dry: bool = False) -> list:
    """Each fix: {op: ledger_stage, ledger, key, stage} | {op: followup_done, id} |
    {op: followup_arm, sop, step, vars} (a mined money date -> an armed reminder, mining.py) |
    {op: hb_stage, workspace_id, stage, desc} (2026-10-18: a HoneyBook pipeline move through hb_exec.py's
    connector template, read back; the one op here that touches HoneyBook. It notifies nobody by itself,
    though the target stage's own HoneyBook automation may run, which the card says).
    Returns one result row per fix. Nothing here sends a message to anyone."""
    results = []
    for fx in fixes:
        op = str(fx.get("op") or "")
        try:
            if op == "hb_stage":
                import json as _json
                import subprocess
                import sys as _sys
                import tempfile
                wid, stage = str(fx.get("workspace_id") or ""), str(fx.get("stage") or "")
                if not re.fullmatch(r"[0-9a-f]{24}", wid) or not stage:
                    results.append({"op": op, "ok": False, "why": "hb_stage needs a 24-hex workspace_id and a stage"}); continue
                with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
                    _json.dump({"workspace_id": wid, "stage": stage}, f)
                    pf = f.name
                cmd = [_sys.executable, str(C.SCRIPTS / "hb_exec.py"), "--kind", "hb_set_stage", "--payload-file", pf] + (["--dry-run"] if dry else [])
                try:
                    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, cwd=str(C.VAULT))
                finally:
                    try:
                        Path(pf).unlink()
                    except OSError:
                        pass
                out = ((r.stdout or "") + (r.stderr or "")).strip()
                ok = r.returncode == 0 and ("HB_DONE=" in out or (dry and "DRY_RUN_OK" in out))
                results.append({"op": op, "ok": ok, "key": fx.get("key"), "workspace_id": wid, "stage": stage,
                                "why": "" if ok else out[-200:], "verified": "VERIFIED=yes" in out, "dry": dry})
            elif op == "ledger_stage":
                path = Path(fx["ledger"])
                led = C.load_json(path, {}) or {}
                p = (led.get("projects") or {}).get(fx["key"])
                if not p:
                    results.append({"op": op, "ok": False, "why": f"no project {fx['key']}"}); continue
                before = p.get("stage")
                if not dry:
                    p["stage"] = fx["stage"]
                    p["last_action"] = C.now_local().strftime("%Y-%m-%d")
                    p["note"] = (str(p.get("note") or "") + f" | stage {before}->{fx['stage']} by tracking_fix {p['last_action']}").strip(" |")
                    C.save_json(path, led)
                results.append({"op": op, "ok": True, "key": fx["key"], "from": before, "to": fx["stage"]})
            elif op == "followup_done":
                import followups
                with followups.StateLock():
                    st = followups.load_state()
                    it = st["items"].get(fx["id"])
                    if not it:
                        results.append({"op": op, "ok": False, "why": f"no follow-up {fx['id']}"}); continue
                    if not dry:
                        it["status"] = "done"
                        followups.save_state(st)
                if not dry:
                    followups.log("mark", it, {"note": "tracking_fix: settled per mail evidence"})
                results.append({"op": op, "ok": True, "id": fx["id"]})
            elif op == "followup_arm":
                import followups
                sop, step, vars_ = str(fx.get("sop") or ""), str(fx.get("step") or ""), dict(fx.get("vars") or {})
                if not sop or not step or not vars_.get("anchor"):
                    results.append({"op": op, "ok": False, "why": "followup_arm needs sop, step and vars.anchor"}); continue
                if dry:
                    results.append({"op": op, "ok": True, "sop": sop, "step": step, "dry": True}); continue
                r = followups.arm(sop, step, vars_, "tracking_fix")
                results.append({"op": op, "ok": bool(r.get("armed") or r.get("id")), "sop": sop, "step": step,
                                "id": r.get("id"), "why": "" if r.get("armed") else str(r.get("reason") or "already armed")[:120]})
            else:
                results.append({"op": op, "ok": False, "why": "unknown op"})
        except Exception as e:  # noqa: BLE001
            results.append({"op": op, "ok": False, "why": f"{type(e).__name__}: {str(e)[:160]}"})
    if not dry:
        latch([f"{'ledger:' + r['key'] if r.get('key') else 'fu:' + r.get('id', '')}" for r in results
               if r.get("ok") and r.get("op") in ("ledger_stage", "followup_done")]
              + [str(r.get("key") or "") for r in results if r.get("ok") and r.get("op") == "hb_stage" and r.get("key")], "fixed")
        C.log("drift_apply", results=results)
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="list", choices=["list", "run", "apply", "drop"])
    ap.add_argument("--fixes-file")
    ap.add_argument("--keys", default="")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.cmd == "run":
        run(); return 0
    if a.cmd == "drop":
        latch([k for k in a.keys.split(",") if k]); print("DROPPED"); return 0
    if a.cmd == "apply":
        fixes = json.loads(Path(a.fixes_file).read_text(encoding="utf-8")) if a.fixes_file else []
        if isinstance(fixes, dict):
            fixes = fixes.get("fixes") or []
        res = apply(fixes, dry=a.dry_run)
        ok = sum(1 for r in res if r.get("ok"))
        print(f"{'DRY_RUN ' if a.dry_run else ''}APPLIED={ok} of {len(res)} " + json.dumps(res, ensure_ascii=False))
        return 0 if ok == len(res) else 1
    for f in findings():
        print(f"{f['lane']:9} {'FIX ' if f.get('fix') else 'ask '} {f['title']}  -- {f['text'][:100]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
