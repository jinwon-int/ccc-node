"""jevlib — shared Jev client + decision ledger (claude/hooks/jev/jevlib).

Contract tests: key resolution order, retry policy (5xx retried, 4xx not),
typed failures, ledger schema/redaction/outcome backfill. Transport is
injected, so no test touches the network.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "claude" / "hooks" / "jev"))

from jevlib import (  # noqa: E402
    JevAPIError,
    JevClient,
    JevUnavailable,
    resolve_key,
)
from jevlib import DecisionLedger, state_hash  # noqa: E402


def ok_transport(body):
    return 200, b'{"answers":{"q1":{"choice":"keep","probability":0.9,"confidence":0.8}}}'


class ClientTest(unittest.TestCase):
    def test_no_key_raises_unavailable(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("TYPESAFE")}
        self.assertIsNone(
            resolve_key(env, env_file="/nonexistent/.env", secret_file="/nonexistent/key")
        )
        client = JevClient(key="")
        with self.assertRaises(JevUnavailable):
            client.ask({}, {"q1": {}})

    def test_resolve_key_from_env(self):
        self.assertEqual(resolve_key({"TYPESAFE_API_KEY": " abc "}), "abc")

    def test_ask_success_and_meta(self):
        client = JevClient(key="k", transport=ok_transport, retries=3)
        answers, meta = client.ask({"hp": 10}, {"q1": {"type": "choice"}})
        self.assertEqual(answers["q1"]["choice"], "keep")
        self.assertEqual(meta["attempts"], 1)
        self.assertGreaterEqual(meta["latency_ms"], 0)

    def test_retry_on_500_then_success(self):
        calls = {"n": 0}

        def flaky(body):
            calls["n"] += 1
            if calls["n"] < 3:
                return 503, b"boom"
            return ok_transport(body)

        client = JevClient(key="k", transport=flaky, retries=3, sleeper=lambda s: None)
        _, meta = client.ask({}, {"q1": {}})
        self.assertEqual(meta["attempts"], 3)

    def test_4xx_no_retry(self):
        calls = {"n": 0}

        def bad(body):
            calls["n"] += 1
            return 401, b"denied"

        client = JevClient(key="k", transport=bad, retries=3, sleeper=lambda s: None)
        with self.assertRaises(JevAPIError):
            client.ask({}, {"q1": {}})
        self.assertEqual(calls["n"], 1)

    def test_choice_wrapper_missing_answer(self):
        client = JevClient(key="k", transport=lambda b: (200, b'{"answers":{}}'))
        with self.assertRaises(JevAPIError):
            client.choice({}, "q1", {"type": "choice"})


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "ledger", "decisions.jsonl")

    def test_append_creates_0600_and_redacts(self):
        led = DecisionLedger(self.path)
        self.assertEqual(oct(os.stat(self.path).st_mode & 0o777), "0o600")
        rid = led.append(
            domain="openmmo.sell",
            session_id="s1",
            decision_point="sell_session",
            state={"gold": 100, "api_key": "SHOULD_NOT_PERSIST"},
            primitive="choice",
            question_id="item_00",
            decision="sell",
            confidence=0.8,
        )
        self.assertEqual(len(rid), 16)
        rec = list(led.iter_records())[0]
        self.assertEqual(rec["schema"], "jev.decision.v1")
        self.assertEqual(rec["state"]["api_key"], "<redacted>")
        self.assertEqual(rec["state"]["gold"], 100)
        s = led.summary()
        self.assertEqual(s["records"], 1)
        self.assertEqual(s["outcomes"]["without"], 1)

    def test_set_outcome_backfills(self):
        led = DecisionLedger(self.path)
        rid = led.append(
            domain="d", session_id="s", decision_point="p", state={"a": 1},
            primitive="choice", question_id="q", decision="keep",
        )
        self.assertTrue(led.set_outcome(rid, {"sold": True, "gold_delta": 5}))
        rec = list(led.iter_records())[0]
        self.assertEqual(rec["outcome"], {"sold": True, "gold_delta": 5})
        self.assertFalse(led.set_outcome("nope", {"x": 1}))

    def test_state_hash_order_insensitive(self):
        self.assertEqual(state_hash({"a": 1, "b": 2}), state_hash({"b": 2, "a": 1}))

    def test_sanitize_deep(self):
        from jevlib import ledger as ledger_mod

        out = ledger_mod.sanitize({"x": [{"token": "t", "ok": 1}], "y": {"PASSWORD": "p"}})
        self.assertEqual(
            out, {"x": [{"token": "<redacted>", "ok": 1}], "y": {"PASSWORD": "<redacted>"}}
        )


if __name__ == "__main__":
    unittest.main()
