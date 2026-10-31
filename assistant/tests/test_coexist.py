"""coexist (2026-10-30): a second Sage agent on this PC never takes the first one's ports, browser, logs or name.

A throwaway home folder with a "first" pack on the old defaults (no state_home: Mission Control 9019, the doer on
9333 with the ~/.claude profile) and an engine checkout (an exported manifest). Checks: the checkout is not a pack;
allocate() picks other ports, its own profile and state_home and refuses the first pack's name; a pack made with those
values has no collision; one sharing the port or profile has, from both sides; orgcfg gives a pack without state_home
no extra environment (the first organization behaves as before) and one with it its own logs, accounts and builder.
   python assistant/tests/test_coexist.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
HOME = _tf.mkdtemp(prefix="wv_coexist_home_")
_os.environ["USERPROFILE"] = _os.environ["HOME"] = HOME
_os.environ["SAGE_PACKS"] = _os.path.join(HOME, ".sage", "packs.json")
import json
import os
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import coexist  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def pack(root: Path, org: dict, doer: dict | None = None, manifest: dict | None = None) -> Path:
    (root / "assistant").mkdir(parents=True, exist_ok=True)
    (root / "assistant" / "org.json").write_text(json.dumps(org), encoding="utf-8")
    (root / "assistant" / "config.json").write_text(json.dumps({"doer": doer or {}}), encoding="utf-8")
    if manifest is not None:
        (root / "engine_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


docs = Path(HOME) / "Documents"
first = pack(docs / "First Org", {"name": "First Org"})
template = pack(docs / "engine-checkout", {"name": "example-org"}, manifest={"engine": {"exported": "2026-10-28"}})
check("an engine checkout is not a pack", not coexist.is_pack(template))
check("the first pack is found by the Documents scan", coexist._norm(first) in [c["root"] for c in coexist.packs()])

new_root = docs / "Second Org"
a = coexist.allocate("Second Org", new_root)
check("a new pack gets another Mission Control port", a["mission_control_port"] != 9019 and a["mission_control_port"] != 9018)
check("a new pack gets another browser port", a["cdp_port"] != 9333)
check("a new pack gets its own browser profile and state home", ".sage" in a["profile"] and a["state_home"] == "~/.sage/second-org")
try:
    coexist.allocate("first org", docs / "Elsewhere")
    check("the first pack's name is refused", False)
except ValueError:
    check("the first pack's name is refused", True)

second = pack(new_root, {"name": "Second Org", "mission_control_port": a["mission_control_port"], "state_home": a["state_home"],
                         "keyring_service": a["keyring_service"]}, {"cdp_port": a["cdp_port"], "profile": a["profile"]},
              {"engine": {"installed": "2026-10-30"}})
coexist.register(second)
check("an installed pack counts as a pack", coexist.is_pack(second))
check("the allocated pack collides with nothing", not coexist.collisions(second))
check("the first pack sees no collision either", not coexist.collisions(first))

clash = pack(docs / "Third Org", {"name": "Third Org", "state_home": "~/.sage/third-org"},
             {"profile": "~/.claude/pw-wv-cli-profile", "cdp_port": a["cdp_port"] + 50})
bad = " | ".join(r["detail"] for r in coexist.collisions(clash))
check("a pack on the default Mission Control port collides", "Mission Control port 9019" in bad, bad)
check("a pack on the first one's signed-in profile collides", "signed-in browser profile" in bad, bad)
check("the first pack sees the clash too", bool(coexist.collisions(first)))

code = ("import sys, json; sys.path.insert(0, sys.argv[1]); import orgcfg; "
        "print(json.dumps({k: __import__('os').environ.get(k) for k in ('WV_LOG_DIR', 'AGENT_ACCOUNT_STORE', 'BUILDER_WT')}))")
def env_of(root: Path) -> dict:
    e = {k: v for k, v in os.environ.items() if k not in ("WV_LOG_DIR", "AGENT_ACCOUNT_STORE", "BUILDER_WT")}
    e["ASSISTANT_ORG"] = str(root / "assistant" / "org.json")
    r = subprocess.run([sys.executable, "-c", code, str(HERE.parent)], capture_output=True, text=True, env=e, timeout=60)
    return json.loads(r.stdout.strip().splitlines()[-1])


e1, e2 = env_of(first), env_of(second)
check("a pack without state_home gets no extra environment", not any(e1.values()), str(e1))
check("a pack with state_home logs under it", str(Path(HOME) / ".sage" / "second-org" / "logs") == e2["WV_LOG_DIR"], str(e2))
check("a pack with state_home has its own Claude account store", "second-org" in str(e2["AGENT_ACCOUNT_STORE"]), str(e2))
check("a pack with state_home has its own builder worktree", "second-org" in str(e2["BUILDER_WT"]), str(e2))

def lock_of(root: Path) -> str:
    e = dict(os.environ, ASSISTANT_ORG=str(root / "assistant" / "org.json"))
    c = "import sys; sys.path.insert(0, sys.argv[1]); sys.argv = ['x']; import bridge; print(bridge.GCHAT_LOCK)"
    r = subprocess.run([sys.executable, "-c", c, str(HERE.parent / "agent")], capture_output=True, text=True, env=e, timeout=120)
    return (r.stdout.strip().splitlines() or [""])[-1]


l1, l2 = lock_of(first), lock_of(second)
check("the first pack's Chat bridge lock stays in ~/.claude", l1 == str(Path(HOME) / ".claude" / "sage-gchat-bridge.lock"), l1)
check("a second pack's Chat bridge lock is its own", l2 == str(Path(HOME) / ".sage" / "second-org" / "sage-gchat-bridge.lock"), l2)

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILED'}")
sys.exit(1 if FAILS else 0)
