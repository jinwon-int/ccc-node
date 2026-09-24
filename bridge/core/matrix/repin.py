"""Audited operator re-pin of the Matrix transport's device trust (#1958).

The saved policy (``meta.policy``) binds the state store to the configured
trust pins, and any config edit makes the next start fail closed with
``saved-policy-changed``. After editing ``devices`` / ``family_devices`` /
``identities`` in the private config, this command adopts exactly those pins
and records who/when/why in ``operator_audit`` (device ids and short key
fingerprints only). Any other policy difference is refused.

Run it with the Matrix service stopped — the state store is single-process
and holds an exclusive lock::

    systemctl stop ccc-matrix-bridge
    # edit the pins in the 0600 config
    <venv>/bin/python -m telegram_bot.core.matrix.repin \\
        --config /etc/family-matrix/config.json --reason "dad's new phone"
    systemctl start ccc-matrix-bridge

Exit codes: 0 re-pinned, 2 refused (invalid config or not a pin change),
3 state store unavailable (locked by the running service, or unsafe).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from telegram_bot.core.matrix.state import MatrixStore, SafetyStop, load_config, operator_name


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m telegram_bot.core.matrix.repin",
        description="Adopt edited Matrix device pins into the saved policy, with an operator audit record.",
    )
    parser.add_argument("--config", type=Path, required=True, help="Private (0600) Matrix config JSON.")
    parser.add_argument("--reason", required=True, help="Why the pins change (kept in operator_audit).")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except (SafetyStop, ValueError) as error:
        print("repin refused: " + str(error), file=sys.stderr)
        return 2
    except OSError as error:
        print("repin refused: config unreadable (" + type(error).__name__ + ")", file=sys.stderr)
        return 2
    if not (Path(config["state_directory"]) / "inbox.sqlite3").is_file():
        # Opening a store creates it; a repin needs an existing saved policy.
        print("repin refused: no state store in state_directory", file=sys.stderr)
        return 2
    try:
        with MatrixStore(config["state_directory"], config["account"]) as store:
            result = store.repin(config, args.reason, operator_name())
    except BlockingIOError:
        print("repin refused: state store is locked; stop the Matrix service first", file=sys.stderr)
        return 3
    except (SafetyStop, ValueError) as error:
        print("repin refused: " + str(error), file=sys.stderr)
        return 2
    except OSError as error:
        print("repin refused: state store unavailable (" + type(error).__name__ + ")", file=sys.stderr)
        return 3
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
