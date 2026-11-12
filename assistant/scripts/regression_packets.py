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
--check lists as OWED the packets with neither. Packets may sit in packets/ or one sub-folder down (packets/<group>/), as
one .json each or as a .jsonl with one packet per line (generated groups).

Suites and scoring (per packet, as data): `suite`: smoke (10-20 packets, the critical paths, on every rules / prompt
commit, a handful live at most) | regression (the core few hundred: on demand before a rules / prompt change ships and
from the nightly post-patch step) | full (every unique packet, weekly at most, never from a session). `scoring`:
deeds (a deterministic action: the replay must show the same action with the same arguments as deed strings, e.g.
exec:wo_close_one + arg:wo=123456-1, no model judge) | judge (open-ended text: phrase checks; an optional judge pass,
never the model configuration that wrote the answer, is the binding's to add). Both land in the ledger row's note.

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

Runner table (v2.2, 2026-11-01): `table_runner({agent: RunnerSpec})` builds the Binding's runner from DATA. Two
shapes of turn: `process` (an argv template run with the lane and the packet file; it prints one `EVAL_RESULT <json>`
line) and `server` (the brain is a chat endpoint: POST {agent: "<brain>-eval-<packet id>", question, <lane field>};
poll a status URL until done; the binding's readers then lift the deeds and the writes out of the lane). Which agent
uses which shape, which endpoint, which brain name and how the lane's leftovers read as deeds are the binding's data;
the engine knows no agent, lane or system of record by name. `--limit N` caps a live run at N turns.
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
import urllib.error
import urllib.request
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
def _first_owner() -> str:
    """The first owner's key (assistant/agent/people.json): the packet's default sender when it names none."""
    try:
        import json as _j
        d = _j.loads((ROOT / "agent" / "people.json").read_text(encoding="utf-8-sig"))
        return next((k for k, u in (d.get("users") or {}).items() if str(u.get("role") or "owner") == "owner"), "owner")
    except Exception:  # noqa: BLE001
        return "owner"


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
    if p.get("suite") not in (None, "") and p.get("suite") not in SUITES:
        probs.append(f"suite must be one of {SUITES}")
    if p.get("scoring") not in (None, "") and p.get("scoring") not in SCORING:
        probs.append(f"scoring must be one of {SCORING}")
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
    """Packets that test nothing yet: no owner sentence AND no expectation (skipped live). A card-made packet with
    deeds or phrases and a null rationale is not owed; it runs on the owner's judgment the card carried."""
    return [p["_name"] for p in packets if not p.get("_error") and p.get("rationale") is None and not has_expectations(p)]


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
def score(reply: str, deeds: list, p: dict, said: list | None = None) -> tuple:
    """(verdict, fail_fields). A deed kind matches by exact name or by family ('card' matches 'card:ask').

    `said` = the texts the turn put in front of the owner besides the reply: the card it posted, the draft it carded
    for a tap. Required phrases are read across the reply AND those texts, because a turn that re-cards an edited
    draft says the edited words on the card and only "nothing sent" in the reply. Forbidden phrases are judged on
    the carded texts alone when there are any (the reply may narrate what it dropped: "his edit removed 'X'"), and
    on the reply when the turn carded nothing."""
    texts = [str(s or "") for s in (said or []) if str(s or "").strip()]
    rl = "\n".join([str(reply or "")] + texts).lower()
    fl = "\n".join(texts).lower() if texts else str(reply or "").lower()
    done = [str(d) for d in (deeds or [])]
    fails = []
    for ph in p.get("forbid_phrases") or []:
        if ph.lower() in fl:
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
    "writes": [abs path], "error": str, "note": str, "said": [str] (optional: the card and draft texts the turn put
    in front of the owner; phrases are scored across reply + said)}; it performs ONE turn with every state write
    pointed into `lane`. record(name, verdict, fail_fields, note, artifact) appends the eval-ledger row. rule_files(tested_rule)
    -> the files that rule is read from. kinds = the deed vocabulary (families or exact kinds)."""
    name: str
    packets_dir: Path
    kinds: set
    runner: Callable | None = None
    record: Callable | None = None
    rule_files: Callable | None = None
    allow_outside: list = field(default_factory=list)
    lane_root: Path | None = None


# ---------------------------------------------------------------- the runner table (agent -> how one turn runs)
@dataclass
class RunnerSpec:
    """How ONE agent's turn runs in a lane, as data.

    mode "process": `command` is an argv template; {lane} and {packet} are filled in (the packet is written into the
    lane as packet.json when it did not arrive as its own .json file); with dry_run the `dry_flag` is appended. The
    process prints one line `EVAL_RESULT <json>` with {reply, deeds, writes, error, server_side, said (optional)}.
    mode "server": the brain is a chat endpoint. POST `endpoint` with {"agent": "<brain>-eval-<packet id>",
    "question": question(packet, lane), <lane_key>: str(lane), **body}, read {"id"}; GET `status` with {id} filled in
    until the document's status is a terminal one (`terminal`). Then deeds(lane, doc) -> [deed],
    writes(lane, doc) -> [abs path] and said(lane, doc) -> [text] read what the turn left behind: the lane's journal,
    the status document's tool steps, whatever the binding decided a deed looks like, the text of every card it
    journalled (so a phrase check sees the carded draft, not only the reply). With dry_run no request is made: dry_seed(lane, packet)
    writes what a canned turn would have left and `dry_answer` stands for the reply, so the readers and the lane
    plumbing run under the tracer with no brain. `server_side` names the writes the server makes that the tracer
    cannot see (they go in the note; {agent} and {id} are filled in)."""
    mode: str = "process"
    command: list = field(default_factory=list)
    dry_flag: str = "--dry-run"
    cwd: str = ""                      # process mode: the working directory (default: this engine's root)
    endpoint: str = ""
    status: str = ""
    brain: str = ""
    body: dict = field(default_factory=dict)
    lane_key: str = "eval_lane"
    question: Callable | None = None
    deeds: Callable | None = None
    writes: Callable | None = None
    said: Callable | None = None
    dry_seed: Callable | None = None
    dry_answer: str = "Noted."
    terminal: tuple = ("done", "error", "failed", "answered", "answer_failed")
    timeout_s: int = 1500
    busy_wait_s: int = 600
    poll_s: float = 5.0
    server_side: list = field(default_factory=list)


def packet_file(packet: dict, lane: Path) -> Path:
    """The packet as its own .json file: the one it came from, or packet.json written into the lane (a packet from a
    JSONL group file has a `file#line` path and no file of its own)."""
    pfile = Path(str(packet.get("_path") or ""))
    if pfile.is_file() and pfile.suffix.lower() == ".json":
        return pfile
    pfile = Path(lane) / "packet.json"
    pfile.write_text(json.dumps({k: v for k, v in packet.items() if not str(k).startswith("_")}, ensure_ascii=False),
                     encoding="utf-8")
    return pfile


def packet_slug(packet: dict) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", str(packet.get("id") or packet.get("_name") or "p").lower()).strip("-")[:40] or "p"


def default_question(packet: dict) -> str:
    """The brain's question when the binding composes none: the history, then the text, with the fixtures named as the
    state this turn must read as current."""
    inp = dict(packet.get("input") or {})
    parts = []
    hist = [str(x) for x in (inp.get("history") or []) if str(x).strip()]
    if hist:
        parts.append("\n".join(hist))
    fx = {k: v for k, v in (packet.get("fixtures") or {}).items()}
    if fx:
        parts.append("[FIXTURES: the records below replace the live state they are named after; read them as current truth]\n"
                     + json.dumps(fx, ensure_ascii=False)[:6000])
    who = str(inp.get("sender") or "").strip()
    role = str(inp.get("role") or "").strip()
    head = f"[from {who}" + (f" (role: {role})" if role else "") + "]\n" if who else ""
    parts.append(head + str(inp.get("text") or ""))
    return "\n\n".join(parts)


def run_process(spec: RunnerSpec, packet: dict, lane: Path, dry_run: bool) -> dict:
    pfile = packet_file(packet, lane)
    cmd = [str(a).replace("{lane}", str(lane)).replace("{packet}", str(pfile)) for a in spec.command]
    if dry_run and spec.dry_flag:
        cmd.append(spec.dry_flag)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=(180 if dry_run else spec.timeout_s), cwd=str(spec.cwd or VAULT))
    except subprocess.TimeoutExpired:
        return {"error": "turn timed out"}
    line = next((ln for ln in (r.stdout or "").splitlines() if ln.startswith("EVAL_RESULT ")), "")
    if not line:
        return {"error": f"no EVAL_RESULT (rc={r.returncode}): {((r.stderr or '') + (r.stdout or ''))[-300:]}"}
    try:
        res = json.loads(line[len("EVAL_RESULT "):])
    except ValueError as e:
        return {"error": f"EVAL_RESULT is not json: {e}"}
    note = ""
    if res.get("server_side"):
        note = "server-side (not traced): " + "; ".join(str(x) for x in res["server_side"])
    return {"reply": res.get("reply") or "", "deeds": list(res.get("deeds") or []), "writes": list(res.get("writes") or []),
            "said": [str(s) for s in (res.get("said") or []) if str(s or "").strip()], "error": res.get("error") or "", "note": note}


def _http_json(url: str, body: dict | None = None, timeout: int = 30):
    """One JSON request; (status code, document). A transport failure is (0, {"error": ...})."""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, method="POST" if body is not None else "GET", data=data,
                                 headers={"Content-Type": "application/json"} if body is not None else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as he:
        try:
            return he.code, json.loads(he.read() or b"{}")
        except ValueError:
            return he.code, {"error": f"HTTP {he.code}"}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": f"{type(e).__name__}: {e}"}


def ask_server(spec: RunnerSpec, agent: str, question: str, lane: Path) -> dict:
    """POST the question, poll the status URL to a terminal state. Returns the final status document; on a transport or
    protocol failure a document {"status": "error", "error": ...}. 409 / 429 (the brain is busy) waits and retries
    for busy_wait_s."""
    body = dict(spec.body or {})
    body.update({"agent": agent, "question": question[:8000]})
    if spec.lane_key:
        body[spec.lane_key] = str(lane)
    t0 = time.time()
    ask_id = ""
    while time.time() - t0 < spec.busy_wait_s:
        code, doc = _http_json(spec.endpoint, body)
        if code in (409, 429):
            time.sleep(min(10.0, spec.poll_s * 2))
            continue
        if code != 200 or not isinstance(doc, dict) or not doc.get("id"):
            return {"status": "error", "error": f"ask refused ({code}): {str((doc or {}).get('error') or doc)[:200]}"}
        ask_id = str(doc["id"])
        break
    if not ask_id:
        return {"status": "error", "error": "the brain stayed busy"}
    t1 = time.time()
    url = spec.status.replace("{id}", ask_id)
    last = {}
    while time.time() - t1 < spec.timeout_s:
        time.sleep(spec.poll_s)
        code, st = _http_json(url)
        if code != 200 or not isinstance(st, dict):
            continue
        last = st
        if str(st.get("status") or "") in spec.terminal:
            st.setdefault("id", ask_id)
            return st
    last = dict(last or {})
    last.update({"status": "error", "error": f"no terminal status in {spec.timeout_s}s (last: {last.get('status')})", "id": ask_id})
    return last


def run_server(spec: RunnerSpec, packet: dict, lane: Path, dry_run: bool) -> dict:
    lane = Path(lane)
    agent = f"{spec.brain}-eval-{packet_slug(packet)}" if spec.brain else packet_slug(packet)
    err = ""
    with WriteTracer() as tr:
        try:
            packet_file(packet, lane)
            q = spec.question(packet, lane) if spec.question else default_question(packet)
            (lane / "question.txt").write_text(q, encoding="utf-8")
            if dry_run:
                doc = {"id": "dryrun", "agent": agent, "status": "done", "answer": str(spec.dry_answer or ""), "steps": []}
                if spec.dry_seed:
                    spec.dry_seed(lane, packet)
            else:
                doc = ask_server(spec, agent, q, lane)
            (lane / "ask_status.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
            deeds = list(spec.deeds(lane, doc) or []) if spec.deeds else []
            extra = list(spec.writes(lane, doc) or []) if spec.writes else []
            said = [str(s) for s in (spec.said(lane, doc) or []) if str(s or "").strip()] if spec.said else []
        except Exception as e:  # noqa: BLE001
            doc, deeds, extra, said = {"status": "error", "answer": ""}, [], [], []
            err = f"{type(e).__name__}: {e}"
    reply = str(doc.get("answer") or "")
    if not err and str(doc.get("status") or "") != "done":
        err = str(doc.get("error") or f"ask ended {doc.get('status')!r}: {reply[:160]}")
    note = ""
    if not dry_run and spec.server_side:
        note = "server-side (not traced): " + "; ".join(
            str(x).replace("{agent}", agent).replace("{id}", str(doc.get("id") or "")) for x in spec.server_side)
    return {"reply": reply, "deeds": deeds, "writes": tr.unique() + extra, "said": said, "error": err, "note": note, "lane_agent": agent}


def table_runner(specs: dict) -> Callable:
    """Binding.runner from {agent: RunnerSpec}: an agent with no row is skipped (never run, never failed)."""
    def runner(packet: dict, lane: Path, dry_run: bool) -> dict:
        agent = str(packet.get("agent") or "").lower()
        spec = specs.get(agent)
        if spec is None:
            return {"skip": f"no runner for agent {agent!r} yet (runners: {sorted(specs)})"}
        if spec.mode == "server":
            return run_server(spec, packet, lane, dry_run)
        return run_process(spec, packet, lane, dry_run)
    return runner


SUITES = ("smoke", "regression", "full")      # smoke: a handful on every rules/prompt commit; regression: the core set
SCORING = ("deeds", "judge")                   # deeds: exact actions + arguments; judge: phrases (+ an optional judge pass)


def live(binding: Binding, packets: list, dry_run: bool = False, only: str = "", agent: str = "", suite: str = "", limit: int = 0) -> list:
    """suite='smoke' runs the smoke packets only; 'regression' runs smoke + regression; 'full' (or '') runs every packet.
    limit=N stops after N turns were actually run (skips do not count): one night's budget for a slow runner."""
    results = []
    ran = 0
    for p in packets:
        if p.get("_error"):
            continue
        if only and p["_name"] != only:
            continue
        if agent and str(p.get("agent") or "") != agent:
            continue
        ps = str(p.get("suite") or "full")
        if suite == "smoke" and ps != "smoke":
            continue
        if suite == "regression" and ps not in ("smoke", "regression"):
            continue
        if p.get("rationale") is None and not has_expectations(p) and not dry_run:
            # a packet generated from a card carries the owner's judgment as deeds and phrases; one with neither a
            # sentence nor an expectation tests nothing and is skipped (the dry run is about isolation and runs all)
            results.append({"packet": p["_name"], "verdict": "SKIP", "note": "no owner rationale and no expectations", "writes_outside": []})
            continue
        if binding.runner is None:
            results.append({"packet": p["_name"], "verdict": "SKIP", "note": "binding has no runner", "writes_outside": []})
            continue
        if limit and ran >= limit:
            results.append({"packet": p["_name"], "verdict": "SKIP", "note": f"over --limit {limit}", "writes_outside": []})
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
        ran += 1
        reply = str(r.get("reply") or "")
        deeds = list(r.get("deeds") or [])
        writes = list(r.get("writes") or [])
        said = [str(s) for s in (r.get("said") or []) if str(s or "").strip()]
        bad = outside(writes, lane, binding.allow_outside)
        if dry_run:
            # the canned answer is not the agent's: a dry run judges isolation only
            verdict, fails = ("LEAK" if bad else "ISOLATED"), []
        else:
            verdict, fails = score(reply, deeds, p, said)
        if r.get("error"):
            verdict, fails = ("LEAK" if dry_run else "FAIL"), fails + [f"error:{str(r['error'])[:80]}"]
        if bad:
            fails = fails + ["isolation:write_outside_lane"]
            verdict = "LEAK" if dry_run else "FAIL"
        note = (f"{time.time() - t0:.0f}s; suite={p.get('suite') or 'full'}; scoring={p.get('scoring') or 'deeds'}; deeds={deeds}; fails={fails}; "
                + (f"outside={bad[:5]}; " if bad else "") + (str(r.get("note") or "") + "; " if r.get("note") else "")
                + f"reply: {reply[:120]!r}")
        row = {"packet": p["_name"], "verdict": verdict, "fail_fields": fails, "deeds": deeds, "writes": writes,
               "writes_outside": bad, "lane": str(lane), "note": note, "reply": reply, "said": said}
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
    ap.add_argument("--suite", default="", choices=("", "smoke", "regression", "full"), help="smoke | regression (smoke+core) | full")
    ap.add_argument("--limit", type=int, default=0, help="with --live: run at most N turns (skips do not count)")
    ap.add_argument("--json", action="store_true", help="machine-readable result")
    a = ap.parse_args(argv)
    packets = load(binding.packets_dir)
    if a.live:
        res = live(binding, packets, dry_run=a.dry_run, only=a.only, agent=a.agent, suite=a.suite, limit=a.limit)
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
        ow = owed(packets)
        for o in ow[:20]:
            print(f"OWED {o}: no rationale and no expectations (skipped live until one exists)")
        if len(ow) > 20:
            print(f"OWED ... {len(ow) - 20} more")
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
    r = subprocess.run([sys.executable, str(ROOT / "agent" / "chat.py"), "--as", str(inp.get("sender") or _first_owner()),
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
