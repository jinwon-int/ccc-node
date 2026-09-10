"""Explicit local attachment to an EXISTING Bot; never a remote reset command."""
from __future__ import annotations

import argparse
import json

from telegram_bot.utils.config import Settings
from .grok_provider import configured_route


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", required=True, help="Dedicated bridge project configuration")
    parser.add_argument("action", choices=("attach-existing", "inspect"))
    parser.add_argument("--acknowledge-existing-context", action="store_true")
    args = parser.parse_args()
    try:
        settings = Settings.load(project_root=args.path)
        route = configured_route(settings)
        if args.action == "attach-existing":
            if not args.acknowledge_existing_context:
                parser.error("attach-existing requires --acknowledge-existing-context; it does not reset the Bot")
            route.journal.create()  # exclusive directory, retains existing/partial state
        with route.journal.claim() as claim:
            current, count, _ = claim.load()
        print(json.dumps({"ok": True, "action": args.action,
                          "session_id": route.journal.binding.session_id,
                          "revisions": count, "stage": None if current is None else current["stage"],
                          "remote_identity_verified": False}))
    except Exception:
        print(json.dumps({"ok": False, "reason": "grok_management_denied_state_retained"}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
