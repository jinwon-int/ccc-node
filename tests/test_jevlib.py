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
        for status in (401, 422, 404):
            calls = {"n": 0}

            def bad(body, _s=status):
                calls["n"] += 1
                return _s, b"denied"

            client = JevClient(key="k", transport=bad, retries=3, sleeper=lambda s: None)
            with self.assertRaises(JevAPIError):
                client.ask({}, {"q1": {}})
            self.assertEqual(calls["n"], 1, f"status {status} must not be retried")

    def test_429_is_retried_then_succeeds(self):
        """Rate limit is the one retryable 4xx — a batch replay depends on it."""
        calls = {"n": 0}

        def throttled(body):
            calls["n"] += 1
            if calls["n"] < 3:
                return 429, b"slow down"
            return ok_transport(body)

        client = JevClient(key="k", transport=throttled, retries=3, sleeper=lambda s: None)
        _, meta = client.ask({}, {"q1": {}})
        self.assertEqual(meta["attempts"], 3)

    def test_429_exhausts_retries_and_raises(self):
        calls = {"n": 0}

        def throttled(body):
            calls["n"] += 1
            return 429, b"slow down"

        client = JevClient(key="k", transport=throttled, retries=3, sleeper=lambda s: None)
        with self.assertRaises(JevAPIError):
            client.ask({}, {"q1": {}})
        self.assertEqual(calls["n"], 3)

    def test_retry_after_header_is_honoured_and_capped(self):
        from jevlib import client as client_mod

        self.assertEqual(client_mod._retry_after_seconds({"Retry-After": "2"}), 2.0)
        self.assertEqual(
            client_mod._retry_after_seconds({"Retry-After": "9999"}),
            client_mod.MAX_RETRY_AFTER,
        )
        # HTTP-date form is unparsed on purpose — fall back to our own backoff
        self.assertIsNone(
            client_mod._retry_after_seconds({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
        )
        self.assertIsNone(client_mod._retry_after_seconds({}))
        self.assertIsNone(client_mod._retry_after_seconds(None))

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

    def test_sanitize_masks_secrets_inside_string_values(self):
        """The key-name pass cannot see a credential sitting in free text.

        Consumers whose state is error prose or fetched content (piri retry
        classification, content screening) hit this on the first provider that
        echoes the request URL back in its error message.
        """
        from jevlib import ledger as ledger_mod

        leak = "401 from https://user:hunter2hunter2@api.vendor.com/v1 (req 7)"
        out = ledger_mod.sanitize({"error_text": leak, "attempt": 2})
        self.assertNotIn("hunter2hunter2", out["error_text"])
        self.assertIn("[REDACTED-SECRET]", out["error_text"])
        self.assertEqual(out["attempt"], 2)
        # non-secret text is untouched, and nesting still works
        nested = ledger_mod.sanitize({"a": [{"msg": "plain text", "t": "ghp_" + "A" * 24}]})
        self.assertEqual(nested["a"][0]["msg"], "plain text")
        self.assertNotIn("AAAA", nested["a"][0]["t"])

    def test_redactor_has_not_drifted_from_the_canonical_masker(self):
        """jevlib copies auto-distill's patterns; this fails if they diverge.

        The copy exists so a distill-side import error can never break a gate.
        The cost of copying is drift, so pin it: improve one, sync the other.

        Compared as source text, not by import: auto-distill.py is a script with
        module-level imports of its own siblings and cannot be imported here,
        which is precisely why jevlib does not depend on it.
        """
        canonical_src = (
            REPO_ROOT / "scripts" / "auto-distill" / "auto-distill.py"
        ).read_text()
        copy_src = (
            REPO_ROOT / "claude" / "hooks" / "jev" / "jevlib" / "redact.py"
        ).read_text()

        start = canonical_src.index("_B = r")
        end = canonical_src.index("re.I)", canonical_src.index("_ASSIGN_RE")) + len("re.I)")
        block = canonical_src[start:end]

        self.assertGreater(len(block), 500, "canonical masker block not located")
        self.assertIn(
            block,
            copy_src,
            "jevlib/redact.py has drifted from scripts/auto-distill/auto-distill.py — "
            "the masker was improved on one side only. Re-copy lines _B..._ASSIGN_RE.",
        )


if __name__ == "__main__":
    unittest.main()
