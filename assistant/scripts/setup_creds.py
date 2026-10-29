"""setup_creds.py - the owner runs this himself, in a terminal. Never run by an agent.

Stores the assistant's two kinds of secret in Windows Credential Manager
(service "the owner-assistant"); nothing is written to this repo.

  setup_creds.py mail personal      -> prompts (hidden) for the Gmail APP PASSWORD
  setup_creds.py calendar personal  -> prompts (hidden) for the calendar's secret iCal URL
  setup_creds.py money simplefin    -> prompts (hidden) for a SimpleFIN SETUP TOKEN, claims the
                                       read-only access URL (the one POST in the protocol) and
                                       stores THAT; the token is single-use and never kept
  setup_creds.py money plaid        -> prompts (hidden) for the Plaid CLIENT ID + SECRET (dashboard.plaid.com,
                                       read-only use), stores them, then opens Plaid Link in the browser so
                                       the owner signs in to Relay THERE (never here); the access token the
                                       Link flow receives is stored as plaid:access_token:<item>
  setup_creds.py status             -> shows which secrets are set (never their values)
  setup_creds.py clear <key>        -> removes one, e.g. imap:personal

App password : https://myaccount.google.com/apppasswords  (needs 2-Step Verification)
Secret iCal  : Google Calendar -> Settings -> <your calendar> -> "Secret address in iCal format"
SimpleFIN    : https://beta-bridge.simplefin.org -> link your banks there -> "New App" -> copy the setup token
Plaid        : https://dashboard.plaid.com -> Team Settings -> Keys (client id + the Production secret)
"""
import getpass
import sys

from alib import KEYRING_SERVICE, config, ensure_venv
ensure_venv()

import keyring  # noqa: E402


def _money_feeds():
    """money.json -> feeds (a list; the older single `feed` block still counts)."""
    try:
        import json
        from pathlib import Path
        m = json.loads((Path(__file__).resolve().parent.parent / "money.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    feeds = [f for f in (m.get("feeds") or []) if isinstance(f, dict)]
    if isinstance(m.get("feed"), dict):
        feeds.append(m["feed"])
    return feeds


def _money_key(provider="simplefin"):
    for f in _money_feeds():
        if f.get("provider", "simplefin") == provider:
            if provider == "plaid":
                return dict(f.get("secrets") or {})
            return str(f.get("secret") or "simplefin:access_url")
    return {"client_id": "plaid:client_id", "secret": "plaid:secret"} if provider == "plaid" else "simplefin:access_url"


def _keys():
    cfg = config()
    pl = _money_key("plaid")
    return ([a["secret"] for a in cfg["mail"]["accounts"]] + [f["secret"] for f in cfg["calendar"]["feeds"]]
            + [_money_key("simplefin"), pl.get("client_id", "plaid:client_id"), pl.get("secret", "plaid:secret")])


def main():
    args = sys.argv[1:]
    if not args or args[0] == "status":
        for k in _keys():
            print(f"{k:<20} {'SET' if keyring.get_password(KEYRING_SERVICE, k) else 'not set'}")
        return
    if args[0] == "clear" and len(args) == 2:
        keyring.delete_password(KEYRING_SERVICE, args[1])
        print(f"cleared {args[1]}")
        return
    if args[0] == "money" and len(args) in (2, 3) and args[1] == "simplefin":
        key = _money_key()
        if len(args) == 3 and args[2] == "--stdin":
            # the chat path (assistant/scripts/connect.py, 2026-10-16): the one-time setup token the owner
            # pasted to Sage arrives on stdin, never on the command line or in a file
            tok = sys.stdin.readline().strip()
        else:
            tok = getpass.getpass("paste the SimpleFIN setup token (hidden): ").strip()
        if not tok:
            sys.exit("nothing entered")
        sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent / "money"))
        import simplefin
        try:
            access = simplefin.claim(tok)
        except Exception as e:  # noqa: BLE001
            sys.exit(f"could not claim the token: {e}")
        keyring.set_password(KEYRING_SERVICE, key, access)
        print(f"stored {key} in Windows Credential Manager (read-only access URL; the setup token is spent)")
        print("next: python assistant/scripts/money/simplefin.py --full   (first pull)")
        return
    if args[0] == "money" and len(args) == 2 and args[1] == "plaid":
        keys = _money_key("plaid")
        cid = getpass.getpass("paste the Plaid client id (hidden): ").strip()
        sec = getpass.getpass("paste the Plaid secret for that environment (hidden): ").strip()
        if not cid or not sec:
            sys.exit("nothing entered")
        keyring.set_password(KEYRING_SERVICE, keys.get("client_id", "plaid:client_id"), cid)
        keyring.set_password(KEYRING_SERVICE, keys.get("secret", "plaid:secret"), sec)
        print("stored the Plaid keys in Windows Credential Manager (read-only use; nothing can move money from here)")
        try:
            go = input("open Plaid Link now so you can sign in to Relay there? [Y/n] ").strip().lower()
        except EOFError:
            go = "n"
        if go in ("", "y", "yes"):
            sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent / "money"))
            import plaid
            r = plaid.setup()
            if not r.get("ok"):
                sys.exit(1)
        else:
            print("next: python assistant/scripts/money/plaid.py setup   (opens Plaid Link; you sign in to Relay there)")
        return
    if args[0] in ("mail", "calendar") and len(args) == 2:
        key = ("imap:" if args[0] == "mail" else "ics:") + args[1]
        if key not in _keys():
            sys.exit(f"{key} is not referenced in assistant/config.json")
        val = getpass.getpass(f"paste the {'app password' if args[0] == 'mail' else 'secret iCal URL'} for {key} (hidden): ").strip()
        if args[0] == "mail":
            val = val.replace(" ", "")
        if not val:
            sys.exit("nothing entered")
        keyring.set_password(KEYRING_SERVICE, key, val)
        print(f"stored {key} in Windows Credential Manager")
        return
    sys.exit(__doc__)


if __name__ == "__main__":
    main()
