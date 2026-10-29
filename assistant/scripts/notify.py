"""notify.py - one card to the owner. Reads the text from stdin.

the owner chose NO Telegram for the assistant (2026-09-22): cards are appended to
assistant/data/cards.jsonl and shown on the mission-control page
(assistant/web/index.html, rebuilt by every tick). Never raises - a notify
failure must not fail the cron."""
import json
import sys

from alib import DATA, now_iso

CARDS = DATA / "cards.jsonl"

if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--file":      # PowerShell 5.1 callers: no stdin encoding games
        try:
            text = open(sys.argv[2], encoding="utf-8-sig").read().strip()[:3500]
        except OSError as e:
            print(f"NOTIFY failed: {e}")
            text = ""
    else:
        text = sys.stdin.read().strip()[:3500]
    if text:
        try:
            DATA.mkdir(parents=True, exist_ok=True)
            with open(CARDS, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": now_iso(), "text": text}, ensure_ascii=False) + "\n")
            print("NOTIFY card written to dashboard")
        except Exception as e:
            print(f"NOTIFY failed: {type(e).__name__}: {e}")
        try:                                                     # and the owners' phones (Google Chat, 2026-10-05)
            import chat_push
            n = chat_push.push_text(text)
            print(f"NOTIFY chat pushed={n}" if n else "NOTIFY chat skipped")
        except Exception as e:
            print(f"NOTIFY chat failed: {type(e).__name__}: {e}")
