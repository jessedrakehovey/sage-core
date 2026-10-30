"""vault_purge.py - remove every bank login from the doer's vault on one owner tap (2026-10-30).

the owner, 2026-10-30: "Promptly purge all bank related log-in info you have unless it is through something like
plaid". The doer may not edit its own password store (policy hook), and the owner wants zero PC use, so this is a
plain script with no model, run as the registered job `vault_purge_banks` (assistant/jobs.json): Sage posts the
card, the owner's tap runs it through drain -> jobs.launch -> job_runner, which says the last line in chat.

What counts as a bank is the job's own `match` list in jobs.json (word matches on an entry's title and URL);
its `keep` list wins (an aggregator like Plaid, a wallet like Venmo). On a run it:
  1. finds every matching entry anywhere in agent.kdbx, the Recycle Bin included,
  2. looks for plain copies of their usernames / passwords in text files under assistant/ (paths only),
  3. deletes the entries outright (never to the Recycle Bin), empties matching ones from the bin, saves,
  4. re-opens the file and checks a search for the removed titles finds nothing,
  5. prints ONE plain line: what was removed, how many logins are left, the check, any plain copies.

  python assistant/doer/vault_purge.py --dry     # what it would remove (titles only), changes nothing
  python assistant/doer/vault_purge.py           # the purge (the job runs this)
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                                   # assistant/
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "scripts"))
import vault  # noqa: E402

JOB = "vault_purge_banks"
TEXT_EXT = {".json", ".jsonl", ".md", ".txt", ".log", ".csv", ".py", ".ps1", ".yaml", ".yml", ".ini", ".cfg", ".html"}
MAX_BYTES = 8 * 1024 * 1024


def terms() -> tuple:
    """(match, keep) from the job's entry in jobs.json; lower-case."""
    import jobs
    j = jobs.find(JOB) or {}
    low = lambda xs: [str(x).strip().lower() for x in (xs or []) if str(x).strip()]  # noqa: E731
    return low(j.get("match")), low(j.get("keep"))


def _hit(text: str, words: list) -> bool:
    t = str(text or "").lower()
    return any(re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", t) for w in words)


def is_bank(e, match: list, keep: list) -> bool:
    hay = f"{e.title or ''} {e.url or ''} {vault.host_of(e.url or '') if e.url else ''}"
    return _hit(hay, match) and not _hit(hay, keep)


def _secrets(e) -> set:
    """The strings a plain copy would carry: username and password, the entry's history too. Short ones match everywhere."""
    out = set()
    for x in [e] + list(getattr(e, "history", None) or []):
        if len(str(x.username or "")) >= 4:
            out.add(str(x.username))
        if len(str(x.password or "")) >= 6:
            out.add(str(x.password))
    return out


def plain_copies(values: set, root: Path = ROOT) -> list:
    """Text files under `root` holding any of `values`, as paths relative to root. Never the value."""
    if not values:
        return []
    found = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in TEXT_EXT or "__pycache__" in p.parts or p.resolve() == Path(__file__).resolve():
            continue
        try:
            if p.stat().st_size > MAX_BYTES:
                continue
            t = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(v in t for v in values):
            found.append(p.relative_to(root).as_posix())
    return found


def _bin_entries(kp) -> set:
    g = kp.recyclebin_group
    return {x.uuid for x in (g.entries if g else [])}


def purge(dry: bool = False, root: Path = ROOT) -> str:
    match, keep = terms()
    if not match:
        return "Nothing removed: the vault_purge_banks job in jobs.json has no match list."
    if not vault.exists():
        return "Nothing removed: there is no login vault on this computer."
    kp = vault._open()
    hits = [e for e in kp.entries if is_bank(e, match, keep)]
    binned = _bin_entries(kp)
    titles = sorted({(e.title or vault.host_of(e.url or "") or "untitled") for e in hits}, key=str.lower)
    if dry:
        return (f"Would remove {len(hits)} bank login{'s' if len(hits) != 1 else ''}: {', '.join(titles) or 'none'}."
                if hits else "No bank logins in the vault.")
    if not hits:
        return f"No bank logins in the vault; {len(vault._group(kp).entries)} logins there, nothing changed."
    values = set().union(*(_secrets(e) for e in hits))
    copies = plain_copies(values, root)
    in_bin = sum(1 for e in hits if e.uuid in binned)
    for e in hits:
        kp.delete_entry(e)                           # outright, not to the Recycle Bin
    kp.save()
    kp = vault._open()                               # read the file back: the check is on disk, not in memory
    left = [e for e in kp.entries if is_bank(e, match, keep)]
    still = [t for t in titles if kp.find_entries(title=t, first=True)]
    n = len(vault._group(kp).entries)
    line = (f"Removed {len(hits)} bank login{'s' if len(hits) != 1 else ''} from the vault ({', '.join(titles)})"
            + (f", {in_bin} of them from its Recycle Bin" if in_bin else "")
            + f"; {n} login{'s' if n != 1 else ''} left. ")
    if left or still:
        line += f"A search still finds {', '.join(still) or str(len(left)) + ' bank entries'}, so it is not done."
    else:
        line += "A search for them now finds nothing."
    if copies:
        line += f" Plain copies of those logins are still in: {', '.join(copies[:8])}" + (f" and {len(copies) - 8} more" if len(copies) > 8 else "") + "."
    else:
        line += " No plain copy of them in the assistant's files."
    return line


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    try:
        print(purge(dry=a.dry))
    except Exception as e:  # noqa: BLE001  one plain line for chat, never a secret
        print(f"The bank login purge stopped on an error ({type(e).__name__}); tap its card again to retry.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
