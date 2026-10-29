"""alib.py - shared helpers for the assistant (queue lock, json io, config, secrets).

Stdlib-only except `keyring` (imported lazily, only by the mail/calendar code paths).
Zero-LLM. Every ledger writer in assistant/ goes through QueueLock - see the two
ledger-race entries in Schema/mistakes.md (2026-08-25, 2026-09-06).
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # assistant/
VAULT = ROOT.parent
DATA = Path(os.environ["ASSISTANT_DATA"]) if os.environ.get("ASSISTANT_DATA") else ROOT / "data"   # tests point this at a scratch dir
OUT = ROOT / "out"
CONFIG = ROOT / "config.json"


def _org(key, default):
    """assistant/org.json (orgcfg.py): the machine values another organization sets at new_org time. The literal
    below each call is this organization's value, so nothing changes here (sage-core, 2026-10-27)."""
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import orgcfg
        return orgcfg.get(key, default)
    except Exception:  # noqa: BLE001
        return default


KEYRING_SERVICE = str(_org("keyring_service", "") or "owner1-assistant")

_PY = str(os.environ.get("ASSISTANT_PY") or _org("python", "") or "")
VENV_PY = Path(os.path.expanduser(_PY)) if _PY else Path.home() / ".venvs" / "wedding-web" / "Scripts" / "python.exe"


def ensure_venv():
    """keyring lives in ~/.venvs/wedding-web, not system python. Scripts that need
    secrets call this first so `python assistant/scripts/mail.py ...` works from
    any shell (the headless operator's allowlist names the plain `python` form)."""
    try:
        import keyring  # noqa: F401
    except ImportError:
        if VENV_PY.exists() and Path(sys.executable).resolve() != VENV_PY.resolve():
            import subprocess
            sys.exit(subprocess.call([str(VENV_PY)] + sys.argv))


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))   # -sig: PS 5.1 writes a BOM
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    """Atomic write; retries on Windows PermissionError (indexer/AV holds the target)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8")
    for i in range(6):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.25 * (i + 1))
    os.replace(tmp, path)


def config():
    cfg = load_json(CONFIG, None)
    if cfg is None:
        sys.exit(f"missing or invalid {CONFIG}")
    return cfg


def business_lane() -> str:
    """The organization's business lane: the FIRST lane in config.json -> lanes (new_org.py writes it first)."""
    try:
        lanes = config().get("lanes") or []
    except SystemExit:
        lanes = []
    return str(lanes[0]) if lanes else "business"


# ------------------------------------------------------------------ the organization's own extensions
# config.json -> plugins.modules names modules under assistant/scripts (the CRM's executor kinds, the standing-texts
# resolver, ...). The engine asks each for a hook by attribute and runs the same with none; an export never carries them.
_PLUGINS: dict = {}


def plugins() -> list:
    """The loaded plugin modules, in config order; one that is not here (or fails to import) is skipped, once."""
    import importlib
    try:
        names = (config().get("plugins") or {}).get("modules") or []
    except SystemExit:
        names = []
    if isinstance(names, str):
        names = [names]
    out = []
    for n in names:
        n = str(n).strip()
        if not n or n.startswith("_"):
            continue
        if n not in _PLUGINS:
            if str(ROOT / "scripts") not in sys.path:
                sys.path.insert(0, str(ROOT / "scripts"))
            try:
                _PLUGINS[n] = importlib.import_module(n)
            except Exception as e:  # noqa: BLE001   a missing extension is not a fault of the engine
                _PLUGINS[n] = None
                try:
                    print(f"PLUGIN_SKIPPED {n}: {type(e).__name__}: {str(e)[:120]}", file=sys.stderr)
                except Exception:  # noqa: BLE001
                    pass
        if _PLUGINS[n] is not None:
            out.append(_PLUGINS[n])
    return out


def plugin_map(attr: str) -> dict:
    """The dict `attr` merged across every plugin (later plugins win)."""
    out = {}
    for m in plugins():
        v = getattr(m, attr, None)
        if isinstance(v, dict):
            out.update(v)
    return out


def plugin_set(attr: str) -> set:
    """The set/list/tuple `attr` united across every plugin."""
    out = set()
    for m in plugins():
        v = getattr(m, attr, None)
        if isinstance(v, (set, list, tuple, frozenset)):
            out |= set(v)
    return out


def plugin_call(fn: str, *args, **kw):
    """Call `fn` on each plugin that has it; the first result that is not None / '' / False wins. Never raises."""
    for m in plugins():
        f = getattr(m, fn, None)
        if not callable(f):
            continue
        try:
            r = f(*args, **kw)
        except Exception as e:  # noqa: BLE001
            try:
                print(f"PLUGIN_ERROR {m.__name__}.{fn}: {type(e).__name__}: {str(e)[:120]}", file=sys.stderr)
            except Exception:  # noqa: BLE001
                pass
            continue
        if r not in (None, "", False):
            return r
    return None


def plugin_each(fn: str, *args, **kw) -> list:
    """Call `fn` on every plugin that has it; the list of results. Never raises."""
    out = []
    for m in plugins():
        f = getattr(m, fn, None)
        if not callable(f):
            continue
        try:
            out.append(f(*args, **kw))
        except Exception as e:  # noqa: BLE001
            try:
                print(f"PLUGIN_ERROR {m.__name__}.{fn}: {type(e).__name__}: {str(e)[:120]}", file=sys.stderr)
            except Exception:  # noqa: BLE001
                pass
    return out


def secret(name):
    """Read a secret from Windows Credential Manager. Returns None when unset -
    callers must degrade to NOT_CONFIGURED, never crash the cron.
    2026-10-24: under a Python without `keyring` (the Store python the operator and the
    CLI use) the read is done by the wedding-web venv in a subprocess, so a card answered
    from any interpreter can still nudge the phone (one text waited 51 min on a
    "no smtp secret" that was really a missing module)."""
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, name)
    except ImportError:
        pass
    except Exception:
        return None
    try:
        if not VENV_PY.exists() or Path(sys.executable).resolve() == VENV_PY.resolve():
            return None
        import subprocess
        r = subprocess.run([str(VENV_PY), "-c", "import keyring,sys;print(keyring.get_password(sys.argv[1], sys.argv[2]) or '', end='')",
                            KEYRING_SERVICE, str(name)], capture_output=True, text=True, timeout=20)
        return (r.stdout or None) if r.returncode == 0 and r.stdout else None
    except Exception:
        return None


class QueueLock:
    """Cross-process mutex for assistant/data. Stale locks (crashed owner) are
    broken after max_age_s.

        with QueueLock("sense_gmail") as ok:
            if not ok: return      # QUEUE_BUSY already printed
    """
    def __init__(self, owner, max_age_s=900, wait_s=20):
        self.path = DATA / ".queue.lock"
        self.owner, self.max_age_s, self.wait_s, self.held = owner, max_age_s, wait_s, False

    def __enter__(self):
        DATA.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + self.wait_s
        while True:
            try:
                with open(self.path, "x", encoding="utf-8") as fd:
                    fd.write(json.dumps({"owner": self.owner, "ts": now_iso(), "pid": os.getpid()}))
                self.held = True
                return True
            except (FileExistsError, PermissionError):
                # PermissionError: on Windows, creating the file while the last holder's delete
                # is still pending. It is contention like any other: wait, never crash the writer.
                try:
                    if time.time() - self.path.stat().st_mtime > self.max_age_s:
                        self.path.unlink()
                except OSError:
                    pass                    # vanished or busy: fall through to the deadline check + sleep
                if time.time() >= deadline:
                    other = load_json(self.path, {})
                    print(f"QUEUE_BUSY: {other.get('owner', '?')} holds the lock "
                          f"(pid {other.get('pid', '?')}) - exiting cleanly")
                    return False
                time.sleep(0.5)

    def __exit__(self, *exc):
        if self.held:
            for i in range(8):              # a reader may hold the file open for a moment (Windows)
                try:
                    self.path.unlink()
                    break
                except FileNotFoundError:
                    break
                except OSError:
                    time.sleep(0.05 * (i + 1))
