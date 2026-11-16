#!/usr/bin/env python
"""transcribe.py - a voice note becomes words, on this computer (2026-11-16).

Someone talks to the agent in chat instead of typing; agent/space_reader.py hands the recording here. The speech
model runs on this machine's processor (faster-whisper), so the recording never leaves it; only the model itself is
fetched, once, the first time a voice note arrives. The package is one of the engine's own (assistant/requirements.txt),
so every install can do this; until 2026-11-16 the only copy of this script lived outside the engine and a new
organization's voice notes were saved and never understood.

  python assistant/scripts/transcribe.py <audio> [--model small.en] [--language en] [--out-dir DIR]
      writes <stem>.txt (the words) and <stem>.timed.txt ([m:ss] per sentence) next to the recording, or in DIR
  python assistant/scripts/transcribe.py --check
      exit 0 when the speech package is installed in THIS interpreter, 1 when it is not

--language auto lets the model hear which language it is (use a model without ".en" for that).
"""
import argparse
import importlib.util
import sys
from pathlib import Path


def fmt_ts(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def available() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio", nargs="?")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--model", default="small.en", help="tiny.en / base.en / small.en / medium.en, or without .en for other languages")
    ap.add_argument("--language", default="en", help="a language code, or auto")
    ap.add_argument("--out-dir", default=None, help="where the text goes (default: beside the recording)")
    args = ap.parse_args()
    if args.check:
        print("ok" if available() else "the speech package (faster-whisper) is not installed in this interpreter")
        return 0 if available() else 1
    if not args.audio:
        ap.error("an audio file is needed")
    src = Path(args.audio)
    if not src.exists():
        print(f"ERROR: not found: {src}", file=sys.stderr)
        return 1
    if not available():
        print("ERROR: the speech package (faster-whisper) is not installed in this interpreter", file=sys.stderr)
        return 3

    from faster_whisper import WhisperModel

    lang = None if str(args.language).lower() in ("", "auto") else args.language
    model_name = args.model
    if lang not in (None, "en") and model_name.endswith(".en"):
        model_name = model_name[:-3]                    # an English-only model cannot hear another language
    print(f"Loading model {model_name} (CPU, int8)...", flush=True)
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    print(f"Transcribing {src.name} ...", flush=True)
    segments, info = model.transcribe(str(src), vad_filter=True, beam_size=5, language=lang)
    print(f"  language={info.language} duration={fmt_ts(info.duration)}", flush=True)

    out_dir = Path(args.out_dir) if args.out_dir else src.parent
    plain_parts, timed_parts = [], []
    for seg in segments:
        text = seg.text.strip()
        plain_parts.append(text)
        timed_parts.append(f"[{fmt_ts(seg.start)}] {text}")
    plain = " ".join(plain_parts)
    (out_dir / f"{src.stem}.txt").write_text(plain + "\n", encoding="utf-8")
    (out_dir / f"{src.stem}.timed.txt").write_text("\n".join(timed_parts) + "\n", encoding="utf-8")
    print(f"Wrote {out_dir / (src.stem + '.txt')} ({len(plain.split())} words)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
