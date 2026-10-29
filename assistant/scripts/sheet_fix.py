"""sheet_fix.py - ONE card for the pending edits to a Google Sheet, with every before-value read live.

The doer could not drive the Sheets UI (2026-10-18: tab clicks timed out, the Name Box appended instead
of jumping), so sheet edits go by API with a card instead: this script turns the edits listed in a
config file (business facts: which sheet, which tabs, which keys; default pricing/sheet_edits.json) into
exact cells by reading each tab live and matching header + key, and posts ONE card that lists every
cell as `Tab!A1: before -> after`. the owner's tap on that card mints a `sheet_set` exec ticket
(exec_store.py); drain.py re-reads each cell, refuses the whole ticket if one changed, then writes
each cell with RAW input (never a formula) and reads it back. This script never writes a cell and
never answers a card.

  python assistant/scripts/sheet_fix.py                 plan from the live sheet, then post the card
  python assistant/scripts/sheet_fix.py --plan          print the plan only (no card)
  python assistant/scripts/sheet_fix.py --auto          spine job sheet-fix: quiet and idempotent. Waits for the
                                                        account's consent (SHEET_SCOPE_MISSING), never posts while a
                                                        card is open or a ticket is queued, stops once the sheet
                                                        holds every edit (state: data/sheet_fix_state.json) or once
                                                        the owner tapped Skip on the card (until the config changes).
  --config <path>                                       another edits file (another sheet, another business)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cards  # noqa: E402
import exec_store as es  # noqa: E402

DEFAULT_CONFIG = HERE.parent.parent / "pricing" / "sheet_edits.json"
STATE = es.DATA / "sheet_fix_state.json"
APPROVE = "Write the cells"
HEADER = "Pricing sheet"
MAX_RANGE = "A1:ZZ1000"


def load_config(path=None) -> dict:
    p = Path(path or DEFAULT_CONFIG)
    cfg = json.loads(p.read_text(encoding="utf-8-sig"))
    for k in ("account", "spreadsheet_id", "edits"):
        if not cfg.get(k):
            raise ValueError(f"{p}: needs {k}")
    cfg["_path"] = str(p)
    return cfg


def edits_sha(cfg: dict) -> str:
    return hashlib.sha256(json.dumps({"id": cfg["spreadsheet_id"], "edits": cfg["edits"]}, sort_keys=True).encode()).hexdigest()[:16]


def _col(j: int) -> str:
    s = ""
    j += 1
    while j:
        j, r = divmod(j - 1, 26)
        s = chr(65 + r) + s
    return s


def _at(row, j):
    return row[j] if j < len(row) else ""


def _hdr(grid) -> list:
    return [re.sub(r"\s+", " ", str(h or "")).strip().lower() for h in (grid[0] if grid else [])]


def _rows_with(g, grid, ki, key) -> list:
    want = g.cell_text(key)
    return [i for i in range(1, len(grid)) if g.cell_text(_at(grid[i], ki)) == want]


def plan(cfg: dict, g=None) -> dict:
    """Read every tab the edits name and turn each edit into exact cells. -> {"cells": [{a1, value, before,
    note}], "skipped": [...already as wanted...], "problems": [...]}. A problem means: post nothing."""
    g = g or es._google_api()
    acct, sid = cfg["account"], cfg["spreadsheet_id"]
    out = {"cells": [], "skipped": [], "problems": []}
    grids = {}
    for e in cfg["edits"]:
        tab = str(e.get("tab") or "")
        if tab not in grids:
            try:
                grids[tab] = g.sheet_get(acct, sid, f"'{tab}'!{MAX_RANGE}")
            except Exception as ex:  # noqa: BLE001
                out["problems"].append(f"{tab}: could not read the tab ({type(ex).__name__}: {str(ex)[:100]})")
                continue
        grid = grids[tab]
        hdr = _hdr(grid)
        if not hdr:
            out["problems"].append(f"{tab}: the tab is empty (no header row)")
            continue
        key_col = str(e.get("key_col") or "").strip().lower()
        if key_col not in hdr:
            out["problems"].append(f"{tab}: no column {key_col!r} in the header {hdr}")
            continue
        ki = hdr.index(key_col)
        if e.get("type") == "set":
            col = str(e.get("col") or "").strip().lower()
            if col not in hdr:
                out["problems"].append(f"{tab}: no column {col!r} in the header {hdr}")
                continue
            ci = hdr.index(col)
            for key in e.get("keys") or []:
                hits = _rows_with(g, grid, ki, key)
                if len(hits) != 1:
                    out["problems"].append(f"{tab}: {len(hits)} rows with {key_col}={key} (need exactly 1)")
                    continue
                i = hits[0]
                before = _at(grid[i], ci)
                a1 = g.a1_join(tab, f"{_col(ci)}{i + 1}")
                if g.cell_text(before) == g.cell_text(e.get("value")):
                    out["skipped"].append(f"{a1} already {g.cell_text(before)} ({key_col} {key})")
                    continue
                out["cells"].append({"a1": a1, "value": e.get("value"), "before": before if before != "" else "",
                                     "note": f"{key_col} {g.cell_text(key)}"})
        elif e.get("type") == "append_row":
            values = {str(k).strip().lower(): v for k, v in (e.get("values") or {}).items()}
            if key_col not in values:
                out["problems"].append(f"{tab}: append_row values must include the key column {key_col!r}")
                continue
            missing = [k for k in values if k not in hdr]
            if missing:
                out["problems"].append(f"{tab}: no column(s) {missing} in the header {hdr}")
                continue
            if _rows_with(g, grid, ki, values[key_col]):
                out["skipped"].append(f"{tab} already has a row with {key_col}={g.cell_text(values[key_col])}")
                continue
            src = None
            if e.get("copy_from") not in (None, ""):
                hits = _rows_with(g, grid, ki, e["copy_from"])
                if len(hits) != 1:
                    out["problems"].append(f"{tab}: {len(hits)} rows with {key_col}={e['copy_from']} to copy from (need exactly 1)")
                    continue
                src = grid[hits[0]]
            last = max((i for i in range(len(grid)) if any(g.cell_text(c) for c in grid[i])), default=0)
            new_row = last + 2                                   # 1-based: the row after the last filled one
            if new_row - 1 < len(grid) and any(g.cell_text(c) for c in grid[new_row - 1]):
                out["problems"].append(f"{tab}: row {new_row} is not empty")
                continue
            for j, h in enumerate(hdr):
                if not h:
                    continue
                if h in values:
                    v = values[h]
                elif src is not None and g.cell_text(_at(src, j)) != "":
                    v = _at(src, j)
                else:
                    continue
                out["cells"].append({"a1": g.a1_join(tab, f"{_col(j)}{new_row}"), "value": v, "before": "",
                                     "note": f"new row, {h}" + ("" if h in values else f" copied from {key_col} {e['copy_from']}")})
        else:
            out["problems"].append(f"{tab}: unknown edit type {e.get('type')!r}")
    return out


def question(cfg: dict, pl: dict, g=None) -> str:
    g = g or es._google_api()
    name = str(cfg.get("sheet_name") or "the sheet")
    lines = [f"Pricing sheet \"{name}\": write these {len(pl['cells'])} cells by API (plain values, no formulas; "
             "each is re-read first, and nothing is written if one changed)."]
    if cfg.get("why"):
        lines.append("Why: " + str(cfg["why"]))
    lines += ["", "[SHEET CELLS]"]                 # the list is the card (EXEC_VERBATIM), not its explanation
    for c in pl["cells"]:
        lines.append(f"{c['a1']}: {g.cell_text(c['before']) or '(empty)'} -> {g.cell_text(c['value'])}" + (f"   ({c['note']})" if c.get("note") else ""))
    if pl.get("skipped"):
        lines += ["", "Already right, left alone: " + "; ".join(pl["skipped"])]
    return "\n".join(lines)


def card(cfg: dict, pl: dict, g=None) -> dict:
    payload = {"account": cfg["account"], "spreadsheet_id": cfg["spreadsheet_id"], "sheet_name": str(cfg.get("sheet_name") or ""),
               "cells": [{"a1": c["a1"], "value": c["value"], "before": c["before"]} for c in pl["cells"]]}
    return {"question": question(cfg, pl, g), "header": HEADER, "lane": str(cfg.get("lane") or "wedding"),
            "details": f"Config {cfg.get('_path', '')}. Each cell is written with Google's RAW input, so a value is stored exactly as "
                       "shown and never parsed as a formula; the sheet is read back after the write.",
            "options": [{"label": APPROVE, "description": "Sage writes exactly these cells by API and reads them back"},
                        {"label": "Skip", "description": "Leave the sheet as it is"}],
            "exec": {"kind": "sheet_set", "approve_label": APPROVE, "payload": payload}}


def open_card(sid: str) -> str:
    """The qid of an open sheet_set card for this sheet, '' when none."""
    for p in cards.pending():
        if p.get("exec_kind") != "sheet_set":
            continue
        c = cards._read(cards.LIVE / f"{p['qid']}.json") or {}
        pay = ((c.get("questions") or [{}])[0].get("exec") or {}).get("payload") or {}
        if str(pay.get("spreadsheet_id")) == str(sid):
            return p["qid"]
    return ""


def queued_ticket(sid: str) -> str:
    for t in es.pending() + es.running():
        if t.get("kind") == "sheet_set" and str((t.get("payload") or {}).get("spreadsheet_id")) == str(sid):
            return t["id"]
    return ""


def _state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1), encoding="utf-8")


def run(cfg: dict, mode: str = "post", g=None) -> tuple[int, str]:
    """mode: post | plan | auto. -> (exit code, the one line said)."""
    g = g or es._google_api()
    sha, sid = edits_sha(cfg), cfg["spreadsheet_id"]
    st = _state()
    same = st.get("sha") == sha
    if mode == "auto" and same and st.get("done"):
        return 0, f"SHEET_FIX done {st['done'][:16]}: the sheet holds every edit in {Path(cfg['_path']).name}"
    if mode == "auto" and same and st.get("declined"):
        return 0, f"SHEET_FIX declined: owner1 tapped Skip on card {st.get('card')} ({st['declined'][:16]}); change {Path(cfg['_path']).name} to ask again"
    ok, why = g.sheet_ready(cfg["account"])
    if not ok:
        return (0 if mode == "auto" else 3), "SHEET_FIX waiting for consent: " + why
    tk = queued_ticket(sid)
    if tk:
        return 0, f"SHEET_FIX waiting: ticket {tk} is queued for the drain"
    qid = open_card(sid)
    if qid:
        return 0, f"SHEET_FIX waiting on card {qid}"
    if same and st.get("card"):
        ans = cards.answer_of(st["card"])
        if ans and ans.strip().lower() != APPROVE.lower():
            st.update(declined=time.strftime("%Y-%m-%dT%H:%M:%S"), answer=ans)
            _save(st)
            return 0, f"SHEET_FIX declined: owner1 tapped {ans!r} on card {st['card']}; change {Path(cfg['_path']).name} to ask again"
    pl = plan(cfg, g)
    if pl["problems"]:
        return (0 if mode == "auto" else 2), "SHEET_FIX cannot plan: " + " | ".join(pl["problems"])[:400]
    if not pl["cells"]:
        _save({"sha": sha, "done": time.strftime("%Y-%m-%dT%H:%M:%S"), "card": st.get("card") if same else "",
               "skipped": pl["skipped"]})
        return 0, "SHEET_FIX nothing to do: " + ("; ".join(pl["skipped"])[:300] or "no edits")
    if mode == "plan":
        return 0, question(cfg, pl, g)
    r = cards.post([card(cfg, pl, g)], "sheet_fix", lane=str(cfg.get("lane") or "wedding"))
    if not r.get("posted"):
        return (0 if mode == "auto" else 4), f"SHEET_FIX card refused: {r.get('reason')}"
    _save({"sha": sha, "card": r["qid"], "posted": time.strftime("%Y-%m-%dT%H:%M:%S"), "cells": len(pl["cells"])})
    return 0, f"SHEET_FIX card {r['qid']} posted: {len(pl['cells'])} cells for owner1's tap" + (f" (chat {', '.join(r.get('chat') or [])})" if r.get("chat") else "")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", default="")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--auto", action="store_true")
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    cfg = load_config(a.config or None)
    code, line = run(cfg, "plan" if a.plan else "auto" if a.auto else "post")
    print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
