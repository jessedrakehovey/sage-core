"""regression_packets.py - fixed replays that a prompt or rule change must not make worse (WP6 2026-10-24; v2 2026-10-31).

A packet is one situation that once went wrong (or must never go wrong), what a good turn DOES and never does, and
the owner's own sentence saying what the right answer is and why. Packets are scored on deeds, not prose quality:

  {
    "id": "two-failed-chases-escalate",          # file stem; the ledger row is pipeline regression-<id>
    "agent": "<agent slug>",                     # which brain the binding asks
    "tested_rule": "chase.channel_switch",       # the rule the packet guards; a change to that rule's file flags it
    "rationale": "<the owner's one sentence: what the right answer is and why>"   # null = the packet is SKIPPED
    "input": {"sender": "<handle>", "role": "<role>", "text": "...", "thread": "t1", "history": ["[09:00 Name] ..."]},
    "fixtures": {"<store>": <json>},             # the lane's state for this turn (the binding knows where each goes)
    "expect_deeds": ["card:ask"],                # deed kinds a good turn makes (binding vocabulary)
    "forbid_deeds": ["post:space"],              # deed kinds a good turn never makes
    "forbid_phrases": ["reassigned"],            # the reply never says these (case-insensitive substrings)
    "require_phrases": []                        # the reply must say these
  }

The older shape ({"message", "who", "expect_deeds", "forbid_phrases"}) still loads: message -> input.text, who ->
input.sender, rationale null.

Why the owner writes the rationale: an expectation the system drafted for itself makes the test self-grading. A
packet generated from the owner's answered card carries that judgment as deeds and phrases, so its rationale may be
null (quoted only when the owner typed a note); a packet with neither a sentence nor an expectation is skipped live.
--check lists every null rationale as owed. Packets may sit in packets/ or one sub-folder down (packets/<group>/), as
one .json each or as a .jsonl with one packet per line (generated groups).

Modes
  --check               zero-LLM: every packet is well-formed, names only deed kinds the binding has, and (with
                        --changed <files>) says which packets a changed file FLAGS for review (tested_rule match)
  --live                one turn per packet through the binding's runner, in a throwaway lane (nothing the turn writes
                        lands in the agent's own state); reply + deeds scored; one eval-ledger row per packet
  --live --dry-run      the same turn with the model replaced by the binding's canned answer: prints every file path
                        the turn wrote and whether each is inside the lane. A path outside = isolation is broken.
                        Run this before the first live run of any binding.

Cadence (the plan, 2026-10-31): change-triggered only. --check on a commit that touches a rules file, a prompt or the
card policy; --live from the nightly self-improvement run's post-patch step and after a prompt or rule change, never
from inside an interactive agent session (a nested CLI there reports "not logged in", a false negative). Never nightly
on its own.

Binding: this file is the engine. An organization binds it with a Binding (packets dir, deed kinds, the runner that
performs one turn in a lane, the ledger recorder, the rule -> files map). The default binding below is this
assistant's own (agent/chat.py through the local channel); a production pack vendors this file unchanged and binds it
from a shim.
"""
from __future__ import annotations

import argparse
import builtins
import glob
import io
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
VAULT = ROOT.parent

REQUIRED = ("id", "agent", "tested_rule", "input", "expect_deeds", "forbid_deeds", "forbid_phrases", "require_phrases")
LIST_FIELDS = ("expect_deeds", "forbid_deeds", "forbid_phrases", "require_phrases")
WRITE_MODES = ("w", "a", "x", "+")


# ---------------------------------------------------------------- packets
def normalize(p: dict) -> dict:
    """The v2 shape from either shape; never mutates the input."""
    d = dict(p)
    if "input" not in d and d.get("message"):
        d["input"] = {"sender": d.get("who") or "", "role": "", "text": d["message"]}
        d.setdefault("tested_rule", "legacy")       # the first generation named no rule
    d.setdefault("input", {})
    d.setdefault("fixtures", {})
    d.setdefault("rationale", None)
    d.setdefault("tested_rule", "")
    d.setdefault("agent", "")
    for k in LIST_FIELDS:
        d.setdefault(k, [])
    return d


def load(packets_dir: Path) -> list:
    out = []
    d = Path(packets_dir)
    files = sorted(glob.glob(str(d / "*.json"))) + sorted(glob.glob(str(d / "*" / "*.json")))
    # generated packets travel as one JSONL per group (one packet per line, named by its id)
    for jf in sorted(glob.glob(str(d / "*.jsonl"))) + sorted(glob.glob(str(d / "*" / "*.jsonl"))):
        for i, ln in enumerate(Path(jf).read_text(encoding="utf-8-sig", errors="replace").splitlines(), 1):
            if not ln.strip():
                continue
            try:
                raw = json.loads(ln)
                if not isinstance(raw, dict):
                    raise ValueError("not an object")
                dd = normalize(raw)
                dd["_name"] = str(dd.get("id") or f"{Path(jf).stem}-{i}")
                dd.setdefault("id", dd["_name"])
                dd["_path"] = f"{jf}#{i}"
                out.append(dd)
            except ValueError as e:
                out.append({"_name": f"{Path(jf).stem}#{i}", "_path": f"{jf}#{i}", "_error": str(e)})
    for f in files:
        name = Path(f).stem
        try:
            raw = json.loads(Path(f).read_text(encoding="utf-8-sig"))
            if not isinstance(raw, dict):
                out.append({"_name": name, "_path": f, "_error": "packet is not a JSON object"})
                continue
            d = normalize(raw)
            d.setdefault("id", name)
            d["_name"] = name
            d["_path"] = f
            out.append(d)
        except ValueError as e:
            out.append({"_name": name, "_path": f, "_error": str(e)})
    return out


def validate(p: dict, kinds: set | None = None) -> list:
    """Problems with one normalized packet ([] = well-formed). kinds = the binding's deed vocabulary; a deed kind
    'card:ask' is known when 'card:ask' or its family 'card' is."""
    if p.get("_error"):
        return [p["_error"]]
    probs = []
    for k in REQUIRED:
        if k not in p:
            probs.append(f"missing field {k}")
    if p.get("id") and p.get("_name") and p["id"] != p["_name"] and "#" not in str(p.get("_path") or ""):
        probs.append(f"id {p['id']!r} differs from the file name {p['_name']!r}")
    inp = p.get("input")
    if not isinstance(inp, dict):
        probs.append("input must be an object")
    elif not str(inp.get("text") or "").strip():
        probs.append("input.text is empty")
    if not isinstance(p.get("fixtures", {}), dict):
        probs.append("fixtures must be an object")
    for k in LIST_FIELDS:
        v = p.get(k, [])
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            probs.append(f"{k} must be a list of strings")
    if not str(p.get("tested_rule") or "").strip():
        probs.append("tested_rule is empty")
    r = p.get("rationale")
    if r is not None and (not isinstance(r, str) or not r.strip()):
        probs.append("rationale must be a non-empty sentence or null")
    if kinds is not None:
        for k in ("expect_deeds", "forbid_deeds"):
            for d in p.get(k, []) or []:
                if isinstance(d, str) and not known_kind(d, kinds):
                    probs.append(f"{k}: unknown deed kind {d!r}")
    both = set(p.get("expect_deeds") or []) & set(p.get("forbid_deeds") or [])
    if both:
        probs.append(f"deeds both expected and forbidden: {sorted(both)}")
    return probs


def known_kind(deed: str, kinds: set) -> bool:
    if deed in kinds:
        return True
    fam = deed.split(":", 1)[0]
    return fam in kinds or (fam + ":*") in kinds


def check(packets: list, kinds: set | None) -> list:
    problems = []
    for p in packets:
        for x in validate(p, kinds):
            problems.append(f"{p['_name']}: {x}")
    return problems


def has_expectations(p: dict) -> bool:
    return any(p.get(k) for k in LIST_FIELDS)


def owed(packets: list) -> list:
    """Packets with no owner sentence yet (well-formed, skipped live)."""
    return [p["_name"] for p in packets if not p.get("_error") and p.get("rationale") is None]


def flag_changed(packets: list, changed: list, rule_files: Callable[[str], list] | None) -> list:
    """Packets whose tested_rule lives in one of the changed files: they want the owner's review before the next
    live run. rule_files(tested_rule) -> the files that rule is read from (binding). Paths compare by suffix, so a
    repository-relative list and an absolute list both match."""
    if not changed or rule_files is None:
        return []
    norm = [str(c).replace("\\", "/").lower() for c in changed]
    out = []
    for p in packets:
        if p.get("_error"):
            continue
        try:
            files = [str(f).replace("\\", "/").lower() for f in (rule_files(str(p.get("tested_rule") or "")) or [])]
        except Exception:  # noqa: BLE001
            files = []
        hit = [c for c in norm if any(c.endswith(f) or f.endswith(c) for f in files if f)]
        if hit:
            out.append({"packet": p["_name"], "tested_rule": p.get("tested_rule"), "changed": hit})
    return out


# ---------------------------------------------------------------- scoring
def score(reply: str, deeds: list, p: dict) -> tuple:
    """(verdict, fail_fields). A deed kind matches by exact name or by family ('card' matches 'card:ask')."""
    rl = str(reply or "").lower()
    done = [str(d) for d in (deeds or [])]
    fails = []
    for ph in p.get("forbid_phrases") or []:
        if ph.lower() in rl:
            fails.append(f"said:{ph}")
    for ph in p.get("require_phrases") or []:
        if ph.lower() not in rl:
            fails.append(f"missing:{ph}")
    for d in p.get("expect_deeds") or []:
        if not any(_deed_match(d, x) for x in done):
            fails.append(f"deed_missing:{d}")
    for d in p.get("forbid_deeds") or []:
        if any(_deed_match(d, x) for x in done):
            fails.append(f"deed_forbidden:{d}")
    if not str(reply or "").strip() and not done:
        fails.append("no_turn")
    return ("FAIL" if fails else "PASS"), fails


def _deed_match(want: str, got: str) -> bool:
    if want == got:
        return True
    if want.endswith(":*"):
        return got.startswith(want[:-1])
    return ":" not in want and got.split(":", 1)[0] == want


# ---------------------------------------------------------------- write tracing (the isolation proof)
class WriteTracer:
    """Records the absolute path of every file this process opens for writing, creates, replaces or removes while
    active. Patches the module-level names the standard library's own writers go through (builtins/io open, os
    replace/rename/remove/unlink/mkdir/makedirs, shutil move/copyfile), so pathlib's write_text and Path.open are
    covered too. Not a sandbox: a write still happens; the tracer only tells you where."""

    def __init__(self):
        self.paths: list = []
        self._saved: dict = {}

    def _note(self, p) -> None:
        try:
            if isinstance(p, int):
                return
            self.paths.append(str(Path(os.fspath(p)).resolve()))
        except Exception:  # noqa: BLE001
            self.paths.append(str(p))

    def __enter__(self):
        tr = self

        def open_w(orig):
            def f(file, mode="r", *a, **k):
                if isinstance(mode, str) and any(c in mode for c in WRITE_MODES):
                    tr._note(file)
                return orig(file, mode, *a, **k)
            return f

        def one(orig):
            def f(p, *a, **k):
                tr._note(p)
                return orig(p, *a, **k)
            return f

        def two(orig):
            def f(src, dst, *a, **k):
                tr._note(dst)
                return orig(src, dst, *a, **k)
            return f

        self._saved = {("builtins", "open"): builtins.open, ("io", "open"): io.open,
                       ("os", "replace"): os.replace, ("os", "rename"): os.rename, ("os", "remove"): os.remove,
                       ("os", "unlink"): os.unlink, ("os", "mkdir"): os.mkdir, ("os", "makedirs"): os.makedirs,
                       ("shutil", "move"): shutil.move, ("shutil", "copyfile"): shutil.copyfile}
        builtins.open = open_w(builtins.open)
        io.open = open_w(io.open)
        os.replace, os.rename = two(os.replace), two(os.rename)
        os.remove, os.unlink = one(os.remove), one(os.unlink)
        os.mkdir, os.makedirs = one(os.mkdir), one(os.makedirs)
        shutil.move, shutil.copyfile = two(shutil.move), two(shutil.copyfile)
        return self

    def __exit__(self, *exc):
        for (mod, name), fn in self._saved.items():
            setattr({"builtins": builtins, "io": io, "os": os, "shutil": shutil}[mod], name, fn)
        return False

    def unique(self) -> list:
        seen, out = set(), []
        for p in self.paths:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return out


def outside(paths: list, lane: Path, allow: list | None = None) -> list:
    """The traced paths that are not under the lane (or under an allowed root, e.g. the process's temp dir)."""
    roots = [str(Path(lane).resolve()).lower()] + [str(Path(a).resolve()).lower() for a in (allow or [])]
    out = []
    for p in paths:
        pl = str(p).lower()
        if not any(pl == r or pl.startswith(r.rstrip("\\/") + os.sep) or pl.startswith(r.rstrip("\\/") + "/") for r in roots):
            out.append(p)
    return out


# ---------------------------------------------------------------- the binding
@dataclass
class Binding:
    """What an organization supplies. runner(packet, lane: Path, dry_run: bool) -> {"reply": str, "deeds": [str],
    "writes": [abs path], "error": str, "note": str}; it performs ONE turn with every state write pointed into
    `lane`. record(name, verdict, fail_fields, note, artifact) appends the eval-ledger row. rule_files(tested_rule)
    -> the files that rule is read from. kinds = the deed vocabulary (families or exact kinds)."""
    name: str
    packets_dir: Path
    kinds: set
    runner: Callable | None = None
    record: Callable | None = None
    rule_files: Callable | None = None
    allow_outside: list = field(default_factory=list)
    lane_root: Path | None = None


def live(binding: Binding, packets: list, dry_run: bool = False, only: str = "", agent: str = "") -> list:
    results = []
    for p in packets:
        if p.get("_error"):
            continue
        if only and p["_name"] != only:
            continue
        if agent and str(p.get("agent") or "") != agent:
            continue
        if p.get("rationale") is None and not has_expectations(p) and not dry_run:
            # a packet generated from a card carries the owner's judgment as deeds and phrases; one with neither a
            # sentence nor an expectation tests nothing and is skipped (the dry run is about isolation and runs all)
            results.append({"packet": p["_name"], "verdict": "SKIP", "note": "no owner rationale and no expectations", "writes_outside": []})
            continue
        if binding.runner is None:
            results.append({"packet": p["_name"], "verdict": "SKIP", "note": "binding has no runner", "writes_outside": []})
            continue
        lane = Path(binding.lane_root or Path(os.environ.get("TMP") or os.environ.get("TEMP") or "/tmp")) / f"eval_lane_{p['_name']}_{int(time.time())}"
        lane.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        try:
            r = binding.runner(p, lane, dry_run) or {}
        except Exception as e:  # noqa: BLE001
            r = {"error": f"{type(e).__name__}: {e}"}
        if r.get("skip"):
            results.append({"packet": p["_name"], "verdict": "SKIP", "note": str(r["skip"])[:200], "writes_outside": []})
            continue
        reply = str(r.get("reply") or "")
        deeds = list(r.get("deeds") or [])
        writes = list(r.get("writes") or [])
        bad = outside(writes, lane, binding.allow_outside)
        if dry_run:
            # the canned answer is not the agent's: a dry run judges isolation only
            verdict, fails = ("LEAK" if bad else "ISOLATED"), []
        else:
            verdict, fails = score(reply, deeds, p)
        if r.get("error"):
            verdict, fails = ("LEAK" if dry_run else "FAIL"), fails + [f"error:{str(r['error'])[:80]}"]
        if bad:
            fails = fails + ["isolation:write_outside_lane"]
            verdict = "LEAK" if dry_run else "FAIL"
        note = (f"{time.time() - t0:.0f}s; deeds={deeds}; fails={fails}; "
                + (f"outside={bad[:5]}; " if bad else "") + (str(r.get("note") or "") + "; " if r.get("note") else "")
                + f"reply: {reply[:120]!r}")
        row = {"packet": p["_name"], "verdict": verdict, "fail_fields": fails, "deeds": deeds, "writes": writes,
               "writes_outside": bad, "lane": str(lane), "note": note, "reply": reply}
        if not dry_run and binding.record is not None:
            try:
                binding.record(p["_name"], verdict, fails, note, p.get("_path") or "")
            except Exception as e:  # noqa: BLE001
                row["note"] += f"; ledger write failed: {e}"
        results.append(row)
    return results


def main(binding: Binding, argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description=f"regression packets ({binding.name})")
    ap.add_argument("--check", action="store_true", help="zero-LLM shape check (default)")
    ap.add_argument("--live", action="store_true", help="one turn per packet in a throwaway lane")
    ap.add_argument("--dry-run", action="store_true", help="with --live: canned answer, print every path written")
    ap.add_argument("--changed", nargs="*", default=[], help="files a commit changed: flags packets by tested_rule")
    ap.add_argument("--only", default="", help="one packet id")
    ap.add_argument("--agent", default="", help="only this agent's packets")
    ap.add_argument("--json", action="store_true", help="machine-readable result")
    a = ap.parse_args(argv)
    packets = load(binding.packets_dir)
    if a.live:
        res = live(binding, packets, dry_run=a.dry_run, only=a.only, agent=a.agent)
        if a.json:
            print(json.dumps(res, ensure_ascii=False, indent=1))
        else:
            for r in res:
                print(f"{r['verdict']} {r['packet']}: {r['note']}")
                if a.dry_run:
                    for w in r.get("writes") or []:
                        mark = "OUTSIDE" if w in (r.get("writes_outside") or []) else "lane   "
                        print(f"    {mark} {w}")
        n_out = sum(1 for r in res if r.get("writes_outside"))
        if a.dry_run:
            print(f"RESULT REGRESSION DRYRUN isolated={sum(1 for r in res if r['verdict'] == 'ISOLATED')} "
                  f"leak={sum(1 for r in res if r['verdict'] == 'LEAK')} writes_outside_lane={n_out}")
            return 1 if any(r["verdict"] == "LEAK" for r in res) else 0
        print(f"RESULT REGRESSION pass={sum(1 for r in res if r['verdict'] == 'PASS')} "
              f"fail={sum(1 for r in res if r['verdict'] == 'FAIL')} skip={sum(1 for r in res if r['verdict'] == 'SKIP')} "
              f"writes_outside_lane={n_out}")
        return 1 if (n_out or any(r["verdict"] == "FAIL" for r in res)) else 0
    probs = check(packets, binding.kinds)
    flags = flag_changed(packets, a.changed, binding.rule_files)
    if a.json:
        print(json.dumps({"problems": probs, "flagged": flags, "owed": owed(packets), "packets": [p["_name"] for p in packets]}, indent=1))
    else:
        for x in probs:
            print("PROBLEM " + x)
        for f in flags:
            print(f"FLAG {f['packet']} (tested_rule {f['tested_rule']}) <- {', '.join(f['changed'])}: review before the next live run")
        for o in owed(packets):
            print(f"OWED {o}: rationale is null (skipped live until the owner writes the sentence)")
        print(f"RESULT REGRESSION packets={len(packets)} problems={len(probs)} flagged={len(flags)} owed={len(owed(packets))}")
    return 1 if probs else 0


# ---------------------------------------------------------------- this assistant's own binding (the default)
def _default_kinds() -> set:
    base = {"task", "do", "note", "decide", "call", "learn", "remember", "event", "schedule", "followup", "skill", "org",
            "switch", "identify", "approve", "reply", "silent"}
    try:
        sys.path.insert(0, str(ROOT / "agent"))
        import intents
        base |= set(getattr(intents, "KEY_OF", {}) or {})
    except Exception:  # noqa: BLE001
        pass
    return base


def _default_runner(p: dict, lane: Path, dry_run: bool) -> dict:
    """One local-channel turn through agent/chat.py (needs the bridge running and the user-env token). The local
    channel keeps its own state; this runner reports the reply only (no lane redirection here: the production packs
    bind their own lane-aware runner)."""
    if dry_run:
        return {"reply": "", "deeds": [], "writes": [], "note": "default binding has no dry-run turn"}
    inp = p.get("input") or {}
    r = subprocess.run([sys.executable, str(ROOT / "agent" / "chat.py"), "--as", str(inp.get("sender") or "owner1"),
                        "--timeout", "300", str(inp.get("text") or "")],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=360, cwd=str(VAULT))
    reply = (r.stdout or "").strip()
    deeds = [] if (not reply or reply.startswith("TIMEOUT")) else ["reply"]
    return {"reply": reply, "deeds": deeds, "writes": []}


def _default_record(name: str, verdict: str, fails: list, note: str, artifact: str) -> None:
    led = VAULT / "scripts" / "evals" / "eval_ledger.py"
    if not led.exists():
        return
    subprocess.run([sys.executable, str(led), "record", "--pipeline", f"regression-{name}", "--artifact", str(artifact),
                    "--verdict", verdict, "--fail-fields", ",".join(fails), "--note", note[:300]],
                   capture_output=True, text=True, timeout=60, cwd=str(VAULT))


def default_binding() -> Binding:
    return Binding(name="assistant", packets_dir=Path(os.environ.get("WV_PACKETS") or (VAULT / "scripts" / "evals" / "packets")),
                   kinds=_default_kinds(), runner=_default_runner, record=_default_record, rule_files=None)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main(default_binding()))
