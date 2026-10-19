"""``telegram_bot.contracts.transport`` is the single definition of the turn
transport seam (#2380 PR-1): the Matrix modules re-export it instead of carrying
mirrors, and the contract imports without any channel SDK.
"""

from __future__ import annotations

import dataclasses
import importlib
import subprocess
import sys
import unittest
from pathlib import Path

from telegram_bot.contracts import transport as contracts

_BRIDGE_DIR = Path(__file__).resolve().parents[1]


class TransportContractRelocationTests(unittest.TestCase):
    def test_matrix_transport_reexports_same_objects(self) -> None:
        matrix_transport = importlib.import_module("telegram_bot.core.matrix.transport")
        for name in ("TurnSink", "TurnResult", "TurnRunner"):
            with self.subTest(name=name):
                self.assertIs(getattr(matrix_transport, name), getattr(contracts, name))

    def test_matrix_bot_and_grok_use_contract_result(self) -> None:
        matrix_bot = importlib.import_module("telegram_bot.core.matrix.bot")
        grok = importlib.import_module("telegram_bot.core.grok_matrix_bot")
        for name in ("TurnSink", "TurnResult", "TransportPort"):
            with self.subTest(module="matrix.bot", name=name):
                self.assertIs(getattr(matrix_bot, name), getattr(contracts, name))
        built = matrix_bot._turn_result("hi", "s1", streamed=True, status="uncertain")
        self.assertIsInstance(built, contracts.TurnResult)
        self.assertEqual(
            (built.text, built.session_id, built.streamed, built.status),
            ("hi", "s1", True, "uncertain"),
        )
        grok_built = grok._turn_result("reply")
        self.assertIsInstance(grok_built, contracts.TurnResult)
        self.assertEqual((grok_built.text, grok_built.session_id), ("reply", None))
        # The mirrors are gone: nothing else named like the contract is defined there.
        self.assertFalse(hasattr(grok, "_TurnResult"))

    def test_turn_result_shape_unchanged(self) -> None:
        fields = [(f.name, f.default) for f in dataclasses.fields(contracts.TurnResult)]
        self.assertEqual(
            fields,
            [
                ("text", dataclasses.MISSING),
                ("session_id", dataclasses.MISSING),
                ("streamed", False),
                ("status", "complete"),
            ],
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            contracts.TurnResult("t", None).text = "x"  # type: ignore[misc]

    def test_protocol_surfaces_unchanged(self) -> None:
        def methods(proto: type) -> set[str]:
            return {n for n, v in vars(proto).items() if callable(v) and not n.startswith("_")}

        self.assertEqual(methods(contracts.TurnSink), {"typing", "interim", "status", "approval"})
        self.assertEqual(methods(contracts.TurnRunner), {"run", "cancel"})
        self.assertEqual(
            methods(contracts.TransportPort),
            {"open", "run", "close", "enqueue_notice", "room_kind"},
        )
        self.assertEqual(
            sorted(contracts.__all__), ["TransportPort", "TurnResult", "TurnRunner", "TurnSink"]
        )

    def test_contract_imports_without_channel_sdks(self) -> None:
        """A fresh interpreter can import the contract with ``nio``/``telegram`` blocked."""

        code = (
            "import sys\n"
            "class _Block:\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name.split('.')[0] in {'nio', 'telegram', 'aiohttp'}:\n"
            "            raise ImportError('blocked: ' + name)\n"
            "        return None\n"
            "sys.meta_path.insert(0, _Block())\n"
            "import telegram_bot.contracts.transport as t\n"
            "print(sorted(t.__all__))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(_BRIDGE_DIR.parent),
            capture_output=True,
            text=True,
            env={
                "PYTHONPATH": str(_BRIDGE_DIR.parent / ".github" / "pythonpath"),
                "PATH": "/usr/bin:/bin",
            },
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("TurnRunner", proc.stdout)


if __name__ == "__main__":
    unittest.main()
