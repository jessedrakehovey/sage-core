"""layout.py - a long message, laid out so it can be read at a glance in Google Chat (2026-11-04).

The owner: "when it posts large amounts of text about what it did it is overwhelming because it is not formatted
nicely and might be tmi". The prompts already ask for short answers; a report that still comes out long (a job's
findings, a checklist, what was tried at a sign-in) used to go up as one wall of text.

Now a message past the limits keeps its OPENING as the message itself (what happened, or the answer: that is also
what a phone notification shows) and the rest goes into a card under it: one section per heading the text already
had, each folded to its first lines behind Google Chat's own "Show more". A sentence that tells the reader what
they can do next ("The quickest way to finish it yourself ...") gets a section of its own, so the ask is never
folded behind the narration.

Nothing is rewritten, nothing is dropped and no word is added: the same text, in the same order. Zero LLM.
The one door is gchat.post / gchat.patch (every text the agent sends goes through them).

  split(text) -> (lead, card)      card is None when the message is short enough to stay plain text
  switches: config.json -> chat_layout {enabled, min_chars, min_lines, lead_chars, max_chars}
"""
from __future__ import annotations

import html
import re

DEFAULTS = {"enabled": True,
            "min_chars": 600,      # shorter than this (and fewer lines than min_lines) stays a plain message
            "min_lines": 10,
            "lead_chars": 320,     # about how much of a long opening paragraph stays in the message itself
            "max_chars": 12000}    # past this the card would be too big: the caller posts plain text in parts
CARD_ID = "more"
MAX_SECTIONS = 12
WIDGET_MAX = 3000

_BULLET = re.compile(r"^(\s*)((?:[-•*])|(?:\d{1,2}[.)]))\s+(\S.*)$")
_HASH = re.compile(r"^\s*#{1,6}\s+(\S.*?)\s*#*\s*$")
_BOLD_LINE = re.compile(r"^\s*(?:\*\*([^*\n]+?)\*\*|\*([^*\n]+?)\*|__([^_\n]+?)__)\s*:?\s*$")
_PAREN = re.compile(r"\([^)]*\)")
# a sentence that hands the next move to the reader: it opens a section of its own and is never folded behind the rest
_NEXT = re.compile(r"^(?:the quickest way|quickest way|your next step|next step|what i need from you|what you can do|to finish it)\b", re.I)
_ABBR = {"e.g", "i.e", "etc", "vs", "mr", "mrs", "ms", "dr", "st", "no", "approx", "a.m", "p.m", "u.s", "inc", "ltd", "co"}


def cfg() -> dict:
    c = dict(DEFAULTS)
    try:
        from alib import config
        v = config().get("chat_layout") or {}
        if isinstance(v, dict):
            c.update(v)
    except Exception:  # noqa: BLE001
        pass
    return c


# ---------------------------------------------------------------- reading the text
def _heading(line: str, following_bullet: bool) -> str:
    """The heading a line is, or ''. A markdown header, a line that is bold from end to end, a short line in capitals,
    or a short "Label:" line directly above a list."""
    s = line.strip()
    if not s or _BULLET.match(line):
        return ""
    m = _HASH.match(line)
    if m:
        return m.group(1).strip().strip("*").strip()
    m = _BOLD_LINE.match(line)
    if m:
        return next(g for g in m.groups() if g).strip().rstrip(":").strip()
    if len(s) <= 60:
        bare = _PAREN.sub("", s).rstrip(":").strip()
        letters = [ch for ch in bare if ch.isalpha()]
        if len(letters) >= 3 and all(ch.isupper() for ch in letters) and not re.search(r"[.!?]$", bare):
            return s.rstrip(":").strip()
        if s.endswith(":") and following_bullet and len(s) <= 40 and not re.search(r"[.!?,;]", s[:-1]):
            return s[:-1].strip()
    return ""


def sentences(text: str) -> list:
    """A paragraph as its sentences. A web address, a decimal, an initial or a common abbreviation does not end one."""
    t = " ".join(str(text or "").split())
    out, start = [], 0
    for m in re.finditer(r"[.!?][\"”’)\]]*\s+", t):
        end = m.end()
        nxt = t[end:end + 1]
        if not nxt or not (nxt.isupper() or nxt.isdigit() or nxt in "\"“‘(["):
            continue
        word = re.split(r"\s", t[start:m.start()])[-1].lower().strip("\"“(")
        if word in _ABBR or len(word) == 1 or word.isdigit():
            continue
        out.append(t[start:end].strip())
        start = end
    if t[start:].strip():
        out.append(t[start:].strip())
    return out


def to_html(s: str) -> str:
    """One line of the agent's text as the small HTML a card's text widget takes: bold, italics and links kept."""
    s = html.escape(str(s or ""), quote=False)
    s = re.sub(r"&lt;(https?://[^|\s&]+)\|([^&]+?)&gt;", r'<a href="\1">\2</a>', s)              # <url|words>
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', s)                 # [words](url)
    s = re.sub(r"(?<![\"=>/\w])(https?://[^\s<]+[^\s<.,;:!?)\"'])", r'<a href="\1">\1</a>', s)   # a bare address
    s = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<![\w*])\*([^*\s][^*\n]*?)\*(?![\w*])", r"<b>\1</b>", s)
    s = re.sub(r"(?<![\w_])__([^_\n]+)__(?![\w_])", r"<b>\1</b>", s)
    s = re.sub(r"(?<![\w_/.:-])_([^_\s][^_\n]*?)_(?![\w_])", r"<i>\1</i>", s)
    return re.sub(r"`([^`\n]+)`", r"\1", s)


def _blocks(text: str) -> list:
    """The text as (kind, value) in order: ('head', title) | ('item', (indent, marker, words)) | ('para', words)."""
    lines = str(text or "").replace("\r\n", "\n").split("\n")
    out, para = [], []

    def flush() -> None:
        if para:
            out.append(("para", " ".join(x.strip() for x in para)))
            para.clear()

    for i, ln in enumerate(lines):
        if not ln.strip():
            flush()
            continue
        nxt = next((x for x in lines[i + 1:] if x.strip()), "")
        h = _heading(ln, bool(_BULLET.match(nxt))) if (out or para) or _HASH.match(ln) or _BOLD_LINE.match(ln) else ""
        if h:                                                  # a heading closes the paragraph above it
            flush()
            out.append(("head", h))
            continue
        m = _BULLET.match(ln)
        if m:
            flush()
            out.append(("item", (len(m.group(1).replace("\t", "  ")), m.group(2), m.group(3).strip())))
            continue
        para.append(ln)
    flush()
    return out


# ---------------------------------------------------------------- the card
def _tp(text: str) -> dict:
    return {"textParagraph": {"text": text}}


def _widgets(lines: list, join: str) -> list:
    """Lines joined into as few text widgets as fit (a widget has a size limit)."""
    out, cur = [], ""
    for ln in lines:
        add = (join if cur else "") + ln
        if cur and len(cur) + len(add) > WIDGET_MAX:
            out.append(_tp(cur))
            cur = ln
        else:
            cur += add
    if cur:
        out.append(_tp(cur))
    return out


def _bare(s: str) -> int:
    return len(re.sub(r"<[^>]+>", "", s))


def _keep(lines: list, most: int, budget: int) -> int:
    """How many lines stay in view above the fold: as many as fit the budget, at least one, at most `most`."""
    n, used = 0, 0
    for ln in lines[:most]:
        if n and used + _bare(ln) > budget:
            break
        used += _bare(ln)
        n += 1
    return max(1, n)


def _section(header: str, lines: list, join: str, keep: int) -> dict:
    sec: dict = {}
    if header:
        sec["header"] = html.escape(header, quote=False)
    size = sum(_bare(x) for x in lines)
    if len(lines) > keep and (len(lines) > keep + 1 or size > 420):
        sec["widgets"] = _widgets(lines[:keep], join)[:1] + _widgets(lines[keep:], join)
        if len(_widgets(lines[:keep], join)) > 1:              # the kept part alone overran one widget: no fold, all shown
            sec["widgets"] = _widgets(lines, join)
        else:
            sec["collapsible"] = True
            sec["uncollapsibleWidgetsCount"] = 1
    else:
        sec["widgets"] = _widgets(lines, join)
    return sec


def _item_html(indent: int, marker: str, words: str) -> str:
    mark = marker if marker[0].isdigit() else "•"
    pad = "&nbsp;&nbsp;&nbsp;&nbsp;" * (1 if indent >= 1 else 0)
    if indent >= 1 and not marker[0].isdigit():
        mark = "◦"
    return f"{pad}{mark} {to_html(words)}"


def split(text: str, conf: dict | None = None) -> tuple:
    """(lead, card). lead is the agent's own opening, as it wrote it (the caller applies its chat markup); card is the
    rest as a Cards v2 card, or None when the message is short, switched off, or too big for a card."""
    c = conf or cfg()
    raw = str(text or "").strip()
    n_lines = sum(1 for x in raw.split("\n") if x.strip())
    if not c.get("enabled", True) or "```" in raw or len(raw) > int(c["max_chars"]) \
            or (len(raw) < int(c["min_chars"]) and n_lines < int(c["min_lines"])):
        return raw, None
    blocks = _blocks(raw)
    # the opening: the paragraphs before the first heading or list
    k = 0
    while k < len(blocks) and blocks[k][0] == "para":
        k += 1
    opening, rest = [b[1] for b in blocks[:k]], blocks[k:]
    lead, carry = "", []                                       # carry: prose that follows the lead, before the first heading
    if opening:
        first = opening[0]
        if len(first) <= int(c["lead_chars"]) * 1.3:
            lead, more = first, opening[1:]
            carry = [("para", p) for p in more]
        else:
            ss = sentences(first)
            take = 1
            while take < len(ss) and take < 2 and len(" ".join(ss[:take + 1])) <= int(c["lead_chars"]):
                take += 1
            lead = " ".join(ss[:take])
            tail = ss[take:]
            carry = [("sent", s) for s in tail] + [("para", p) for p in opening[1:]]
    else:                                                      # it opens on a heading or a list: that first line is the lead
        kind, val = rest[0]
        lead = val if kind == "head" else f"{val[1] if val[1][0].isdigit() else chr(0x2022)} {val[2]}"
        rest = rest[1:]

    # sections: the carried prose first, then one per heading
    groups, cur = [], {"header": "", "rows": list(carry)}
    for kind, val in rest:
        if kind == "head":
            if cur["rows"] or cur["header"]:
                groups.append(cur)
            cur = {"header": val, "rows": []}
        else:
            cur["rows"].append((kind, val))
    if cur["rows"] or cur["header"]:
        groups.append(cur)
    # a lone long paragraph reads as sentences; a sentence that hands over the next move opens its own group
    shaped = []
    for g in groups:
        rows = []
        for kind, val in g["rows"]:
            if kind == "para" and len(val) > 420 and not any(r[0] == "item" for r in g["rows"]):
                rows += [("sent", s) for s in sentences(val)]
            else:
                rows.append((kind, val))
        part = {"header": g["header"], "rows": [], "ask": False}
        for kind, val in rows:
            if kind in ("sent", "para") and _NEXT.match(val) and part["rows"]:
                shaped.append(part)
                part = {"header": "", "rows": [], "ask": True}
            elif kind in ("sent", "para") and _NEXT.match(val):
                part["ask"] = True
            part["rows"].append((kind, val))
        shaped.append(part)
    # closing words after the last list (a caveat, an offer) stay in view: a last section of their own
    if shaped and any(r[0] == "item" for r in shaped[-1]["rows"]):
        tail = []
        while shaped[-1]["rows"] and shaped[-1]["rows"][-1][0] != "item":
            tail.insert(0, shaped[-1]["rows"].pop())
        if tail:
            shaped.append({"header": "", "rows": tail, "ask": False})
    shaped = [g for g in shaped if g["rows"] or g["header"]]
    if len(shaped) > MAX_SECTIONS:                             # the overflow rides in the last section, headings as bold lines
        extra = shaped[MAX_SECTIONS - 1:]
        merged = {"header": extra[0]["header"], "rows": list(extra[0]["rows"]), "ask": False}
        for g in extra[1:]:
            if g["header"]:
                merged["rows"].append(("para", f"**{g['header']}**"))
            merged["rows"] += g["rows"]
        shaped = shaped[:MAX_SECTIONS - 1] + [merged]
    body_chars = sum(len(str(v if k != "item" else v[2])) for g in shaped for k, v in g["rows"])
    if not shaped or body_chars < 150:
        return raw, None
    keep_list = 3 if len(shaped) <= 2 else (2 if len(shaped) <= 4 else 1)
    sections = []
    for g in shaped:
        has_items = any(r[0] == "item" for r in g["rows"])
        only_sent = all(r[0] == "sent" for r in g["rows"])
        lines = [_item_html(*v) if kind == "item" else to_html(v) for kind, v in g["rows"]]
        if not lines:
            lines = [""]
        join = "<br>" if has_items else (" " if only_sent else "<br><br>")
        if g["ask"] and only_sent:
            keep = _keep(lines, 3, 340)                        # the reader's next move: in view, folded only where it runs long
        elif has_items:
            keep = _keep(lines, keep_list, 240)
        elif only_sent:
            keep = _keep(lines, 2, 260)
        else:
            keep = 1
        sections.append(_section(g["header"], lines, join, keep))
    return lead, {"cardId": CARD_ID, "card": {"sections": sections}}


def plain(card: dict | None) -> str:
    """The card's words as plain text (tests, and the record of what was posted)."""
    out = []
    for sec in ((card or {}).get("card") or {}).get("sections") or []:
        if sec.get("header"):
            out.append(html.unescape(sec["header"]))
        for w in sec.get("widgets") or []:
            t = (w.get("textParagraph") or {}).get("text") or ""
            t = re.sub(r"<br\s*/?>", "\n", t)
            out.append(html.unescape(re.sub(r"<[^>]+>", "", t)).replace("\xa0", " "))
    return "\n".join(out)
