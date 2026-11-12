"""causes.py - WHY a chase stalled, as a label the owner chose when the item closed. Never inferred by a model.

Seed E of the agent-intelligence plan ("causes", RESHAPE: label first, learn later). The "why" column on a closed
chase is the one thing that could teach an agent something that transfers (this vendor goes quiet; that step
always waits on a part). The roast dropped three things, for now: a cause a model infers (a durable, confident,
wrong label), a priors file formed from one to three closes, and any cause that characterises the people the
organization serves (a per-household "unreachable" count is a characterisation of a household; the fair-treatment
rules apply to internal notes too). What is left is small:

  the vocabulary is DATA             a short list of labels (≤ 6 + skip) in a JSON file the pack owns; this module
                                     carries a default so it runs with none
  a cause is a TAP, or TYPED         label_close() writes `cause` only when `source` is a human source ("tap" or
                                     "typed"); any other source is refused; skip writes nothing at all
  the validator is the wall          a label outside the vocabulary is refused; a label or a free text that names
                                     the people served (the `never` stems) is refused, whatever the source
  coverage() is the read-back        how many closes carry a cause, whether the free texts cluster into the
                                     vocabulary (keyword hints as data, zero-LLM), how many items with nobody to
                                     chase carry one; coverage_words() says it in plain words

  load_vocab(path=None)              -> {"labels": [...], "skip": "skip", "never": [...], "words": {}, "hints": {}, "refused": [...]}
  validate(cause, vocab)             -> (ok: bool, why: str)         a label, skip, or a refusal in words
  check_text(text, vocab)            -> why ("" when the free text is allowed)
  label_close(item, cause, source, ts=None, vocab=None)  -> item   (raises CauseError when refused)
  parse_typed(text)                  -> the free-text cause in a typed line ("stalled: waited on the part") or ""
  cluster(text, vocab)               -> the label a free text reads like, or ""
  coverage(items, vocab, closed_statuses, who_key)       -> dict
  coverage_words(rep)                -> plain words

  python causes.py coverage <state.json> [--vocab <vocab.json>] [--closed answered,parked] [--who vendor]

Pure functions over dicts; no I/O except the CLI. Item fields written: cause, cause_source, cause_ts (a label) or
cause_text, cause_text_ts, cause_source (a free text). Nothing here sends, routes, or mints a card.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

HUMAN_SOURCES = ("tap", "typed")
SKIP = "skip"
# the six labels of the plan; the pack's JSON may rename or shorten the list, never add a people-attributed one
DEFAULT_LABELS = ["vendor_no_reply", "vendor_no_show", "parts_wait", "approval_waited", "key_missing", "wrong_channel"]
# a cause is never about the people the organization serves: a label or a free text carrying one of these stems is
# refused whatever the source (the pack's JSON may add stems; it cannot remove the rule)
DEFAULT_NEVER = ["resident", "tenant", "household", "occupant", "renter", "lessee", "customer", "client"]
DEFAULT_WORDS = {"vendor_no_reply": "the vendor did not reply", "vendor_no_show": "the vendor did not show",
                 "parts_wait": "waited on parts", "approval_waited": "waited on an approval",
                 "key_missing": "no key", "wrong_channel": "asked on the wrong channel", SKIP: "skipped"}
DEFAULT_HINTS = {"vendor_no_reply": ["no reply", "no answer", "never answered", "didn't answer", "did not answer", "silent",
                                     "ghosted", "no response", "not responding", "never got back", "hasn't replied", "has not replied"],
                 "vendor_no_show": ["no show", "no-show", "didn't show", "did not show", "never came", "didn't come", "did not come",
                                    "never showed", "didn't turn up", "did not turn up"],
                 "parts_wait": ["part", "parts", "backorder", "back order", "ordered", "shipping", "delivery", "material", "supplies"],
                 "approval_waited": ["approval", "approve", "approved", "sign off", "sign-off", "ok from", "waiting on me", "my ok",
                                     "bid", "quote", "estimate"],
                 "key_missing": ["key", "keys", "lock", "locked", "access", "no entry", "couldn't get in", "could not get in", "gate code"],
                 "wrong_channel": ["wrong channel", "wrong number", "text instead", "email instead", "call instead", "didn't see",
                                   "did not see", "never saw", "missed the message", "other phone", "wrong thread", "wrong person"]}
MAX_TEXT = 200
_TYPED = re.compile(r"^\s*(?:cause|stalled|stall|why|reason)\s*[:\-]\s*(.+?)\s*$", re.I | re.S)
_LABEL = re.compile(r"^[a-z][a-z0-9_]{1,39}$")


class CauseError(ValueError):
    """A cause that may not be written: outside the vocabulary, about the people served, or not a human choice."""


# ------------------------------------------------------------------ the vocabulary (data)
def _norm(s) -> str:
    return re.sub(r"[\s\-]+", "_", str(s or "").strip().lower())


def _stem_hit(text: str, stems) -> str:
    """The first `never` stem the text carries as a word (or a word's start), '' when none."""
    low = str(text or "").lower()
    for st in stems or ():
        st = str(st or "").strip().lower()
        if st and re.search(r"(?<![a-z])" + re.escape(st), low):
            return st
    return ""


def load_vocab(path=None) -> dict:
    """The vocabulary from a JSON file ({"labels": [...], "skip": "skip", "never": [...], "words": {...}, "hints": {...}})
    merged over the defaults; with no path or an unreadable file, the defaults. A label that carries a `never` stem is
    DROPPED at load and listed under "refused", so a pack cannot add one by editing the file. More than six labels
    keeps the first six (the plan's ceiling: a close card with a row of six taps or skip)."""
    raw: dict = {}
    if path:
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    never = list(DEFAULT_NEVER)
    for st in (raw.get("never") or []):
        if str(st).strip() and str(st).strip().lower() not in never:
            never.append(str(st).strip().lower())
    skip = _norm(raw.get("skip") or SKIP) or SKIP
    labels, refused = [], []
    for lab in (raw.get("labels") if isinstance(raw.get("labels"), list) else DEFAULT_LABELS):
        lab = _norm(lab)
        if not lab or lab == skip or lab in labels:
            continue
        if not _LABEL.match(lab):
            refused.append({"label": lab, "why": "not a label (letters, digits, underscores)"})
            continue
        hit = _stem_hit(lab.replace("_", " "), never)
        if hit:
            refused.append({"label": lab, "why": f"a cause is never about the people served ('{hit}')"})
            continue
        labels.append(lab)
    if len(labels) > 6:
        refused += [{"label": x, "why": "beyond the sixth label"} for x in labels[6:]]
        labels = labels[:6]
    words = dict(DEFAULT_WORDS)
    for k, v in (raw.get("words") or {}).items() if isinstance(raw.get("words"), dict) else ():
        words[_norm(k)] = str(v or "")
    words.setdefault(skip, "skipped")
    hints = {k: list(v) for k, v in DEFAULT_HINTS.items()}
    for k, v in (raw.get("hints") or {}).items() if isinstance(raw.get("hints"), dict) else ():
        if isinstance(v, list):
            hints[_norm(k)] = [str(x).lower() for x in v if str(x).strip()]
    return {"labels": labels, "skip": skip, "never": never, "words": words,
            "hints": {k: v for k, v in hints.items() if k in labels}, "refused": refused}


def vocab_or_default(vocab: dict | None) -> dict:
    return vocab if isinstance(vocab, dict) and vocab.get("labels") is not None else load_vocab()


# ------------------------------------------------------------------ the validator
def validate(cause, vocab: dict | None = None) -> tuple:
    """(ok, why): a label of the vocabulary or skip is ok; anything else is refused in words. A label naming the people
    served is refused even if someone put it in the file (load_vocab dropped it; this is the second wall)."""
    v = vocab_or_default(vocab)
    if not isinstance(cause, str):
        return False, "a cause is a label, not a " + type(cause).__name__
    lab = _norm(cause)
    if not lab:
        return False, "no cause given"
    hit = _stem_hit(lab.replace("_", " "), v["never"])
    if hit:
        return False, f"a cause is never about the people served ('{hit}')"
    if lab == v["skip"] or lab in v["labels"]:
        return True, ""
    return False, f"'{lab}' is not in the vocabulary ({', '.join(v['labels'])} or {v['skip']})"


def check_text(text, vocab: dict | None = None) -> str:
    """Why a free text may not be written ('' when it may): empty, too long after trimming, or about the people served."""
    v = vocab_or_default(vocab)
    if not isinstance(text, str) or not text.strip():
        return "no cause given"
    hit = _stem_hit(text, v["never"])
    if hit:
        return f"a cause is never about the people served ('{hit}')"
    return ""


# ------------------------------------------------------------------ the writer
def _stamp(ts) -> str:
    if isinstance(ts, datetime):
        return ts.strftime("%Y-%m-%dT%H:%M:%S")
    return str(ts or datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))


def label_close(item: dict, cause, source: str, ts=None, vocab: dict | None = None) -> dict:
    """Write the owner's cause on the item. `source` must be a human source: "tap" (a label from the card's row) or
    "typed" (a label or a free text the owner typed). Any other source, a label outside the vocabulary, or a text about
    the people served raises CauseError and writes nothing. Skip writes nothing and returns the item unchanged. A
    label replaces an earlier free text (the owner's later, more precise word wins); a free text never overwrites a
    label already chosen."""
    if not isinstance(item, dict):
        raise CauseError("a cause goes on a record (dict)")
    src = str(source or "").strip().lower()
    if src not in HUMAN_SOURCES:
        raise CauseError(f"a cause is written only from a human choice (tap or typed), not from '{src or 'nothing'}'")
    v = vocab_or_default(vocab)
    if not isinstance(cause, str):
        raise CauseError("a cause is a label or a short text, not a " + type(cause).__name__)
    lab = _norm(cause)
    if lab == v["skip"]:
        return item
    ok, why = validate(lab, v)
    if ok:
        item["cause"] = lab
        item["cause_source"] = src
        item["cause_ts"] = _stamp(ts)
        item.pop("cause_text", None)
        item.pop("cause_text_ts", None)
        return item
    if src != "typed":
        raise CauseError(why)
    # typed and not a label: the free text of the shadow test
    text = re.sub(r"\s+", " ", str(cause)).strip()[:MAX_TEXT]
    bad = check_text(text, v)
    if bad:
        raise CauseError(bad)
    if item.get("cause"):
        return item
    item["cause_text"] = text
    item["cause_text_ts"] = _stamp(ts)
    item["cause_source"] = src
    return item


def parse_typed(text) -> str:
    """The cause in a typed line: 'stalled: waited on the part' / 'cause - vendor no reply' -> the part after the
    marker; '' when the line is not one. A reply that merely mentions the word is not a cause."""
    if not isinstance(text, str):
        return ""
    m = _TYPED.match(text)
    return re.sub(r"\s+", " ", m.group(1)).strip()[:MAX_TEXT] if m else ""


# ------------------------------------------------------------------ clustering free text (keywords as data, zero-LLM)
def cluster(text, vocab: dict | None = None) -> str:
    """The vocabulary label a free text reads like: the label itself written in words ('vendor no reply'), or the label
    whose hints the text carries most (ties: the first label in the list). '' when nothing matches."""
    v = vocab_or_default(vocab)
    low = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    if not low:
        return ""
    if _norm(low) in v["labels"]:
        return _norm(low)
    best, best_n = "", 0
    for lab in v["labels"]:
        n = 0
        for h in v["hints"].get(lab) or []:
            if h and re.search(r"(?<![a-z])" + re.escape(h) + r"(?![a-z])", low):
                n += 1
        if n > best_n:
            best, best_n = lab, n
    return best


# ------------------------------------------------------------------ the read-back
def coverage(items, vocab: dict | None = None, closed_statuses=("answered", "parked", "settled", "closed"),
             who_key: str = "vendor") -> dict:
    """How the labels are being used. items = {key: row} or [row]. A close is a row whose status is one of
    closed_statuses. who_key names the field that says who was being chased; a row with it empty is one with nobody
    to chase (the plan asks how many of those carry a cause at all)."""
    v = vocab_or_default(vocab)
    rows = list(items.values()) if isinstance(items, dict) else list(items or [])
    rows = [r for r in rows if isinstance(r, dict)]
    closed = [r for r in rows if str(r.get("status") or "") in closed_statuses]
    rep = {"items": len(rows), "closes": len(closed), "closes_with_cause": 0, "closes_with_text": 0, "closes_without": 0,
           "by_label": {}, "by_source": {}, "free_texts": [], "clustered": 0, "unclustered": 0,
           "no_who": 0, "no_who_with_cause": 0, "open_with_cause": 0, "invalid": []}
    for r in closed:
        c, t = r.get("cause"), r.get("cause_text")
        if c:
            ok, why = validate(c, v)
            if not ok:
                rep["invalid"].append({"cause": str(c), "why": why})
            rep["closes_with_cause"] += 1
            rep["by_label"][str(c)] = rep["by_label"].get(str(c), 0) + 1
            src = str(r.get("cause_source") or "?")
            rep["by_source"][src] = rep["by_source"].get(src, 0) + 1
        elif t:
            rep["closes_with_text"] += 1
            lab = cluster(t, v)
            rep["free_texts"].append({"text": str(t)[:80], "reads_like": lab})
            if lab:
                rep["clustered"] += 1
            else:
                rep["unclustered"] += 1
        else:
            rep["closes_without"] += 1
        if not str(r.get(who_key) or "").strip():
            rep["no_who"] += 1
            if c or t:
                rep["no_who_with_cause"] += 1
    for r in rows:
        if r not in closed and (r.get("cause") or r.get("cause_text")):
            rep["open_with_cause"] += 1
    n = rep["closes"]
    labelled = rep["closes_with_cause"] + rep["closes_with_text"]
    rep["coverage_pct"] = int(round(100.0 * labelled / n)) if n else 0
    rep["digest_bar_pct"] = 60
    rep["digest_bar_met"] = bool(n) and rep["coverage_pct"] >= 60
    return rep


def _n(n: int, one: str, many: str = "") -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def coverage_words(rep: dict, who_word: str = "vendor") -> str:
    """The read-back in plain words, for a person who reads results as sentences."""
    n = int(rep.get("closes") or 0)
    if not n:
        return "No closed chases yet, so nothing to read back."
    c, t = int(rep.get("closes_with_cause") or 0), int(rep.get("closes_with_text") or 0)
    parts = [f"{c + t} of {_n(n, 'close')} carry a cause ({rep.get('coverage_pct', 0)} percent): {c} as a label, {t} as free text."]
    if rep.get("by_label"):
        top = sorted(rep["by_label"].items(), key=lambda kv: -kv[1])
        parts.append("Labels: " + ", ".join(f"{k} {v}" for k, v in top) + ".")
    if t:
        cl, un = int(rep.get("clustered") or 0), int(rep.get("unclustered") or 0)
        parts.append(f"Of the {_n(t, 'free text')}, {cl} read like a vocabulary label and {un} do not.")
        odd = [x["text"] for x in (rep.get("free_texts") or []) if not x.get("reads_like")][:3]
        if odd:
            parts.append("Not clustering: " + "; ".join(f"'{x}'" for x in odd) + ".")
    nw, nwc = int(rep.get("no_who") or 0), int(rep.get("no_who_with_cause") or 0)
    if nw:
        parts.append(f"{_n(nw, 'close')} had no {who_word} to chase; {nwc} of those carry a cause.")
    if rep.get("invalid"):
        parts.append(f"{_n(len(rep['invalid']), 'cause')} outside the vocabulary (should not happen): "
                     + "; ".join(x["cause"] for x in rep["invalid"][:3]) + ".")
    if rep.get("open_with_cause"):
        parts.append(f"{_n(int(rep['open_with_cause']), 'open item')} already carry a cause (labelled before the close).")
    parts.append("The digest's bar is 60 percent coverage: " + ("reached." if rep.get("digest_bar_met") else "not reached yet."))
    return " ".join(parts)


# ------------------------------------------------------------------ CLI
def main(argv=None) -> int:
    a = list(argv if argv is not None else sys.argv[1:])
    if len(a) < 2 or a[0] != "coverage":
        print(__doc__)
        return 2
    vocab_path, closed, who = None, ("answered", "parked", "settled", "closed"), "vendor"
    i = 2
    while i < len(a):
        if a[i] == "--vocab" and i + 1 < len(a):
            vocab_path, i = a[i + 1], i + 2
        elif a[i] == "--closed" and i + 1 < len(a):
            closed, i = tuple(x.strip() for x in a[i + 1].split(",") if x.strip()), i + 2
        elif a[i] == "--who" and i + 1 < len(a):
            who, i = a[i + 1], i + 2
        else:
            i += 1
    try:
        d = json.loads(Path(a[1]).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        print(f"cannot read {a[1]}: {e}")
        return 1
    items = d.get("items") if isinstance(d, dict) and "items" in d else d
    rep = coverage(items, load_vocab(vocab_path), closed, who)
    print(coverage_words(rep, who))
    print(json.dumps({k: v for k, v in rep.items() if k != "free_texts"}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
