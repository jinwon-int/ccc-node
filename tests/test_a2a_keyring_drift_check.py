"""Key rotation must neither create historical-key alarms nor hide active drift."""
import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "keyring_drift", Path(__file__).resolve().parents[1] / "scripts/a2a-keyring-drift-check.py"
)
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)
V1 = "worker:alpha:g2:v1"
V2 = "worker:alpha:g2:v2"
OTHER = "worker:beta:g2:v1"
OLD = b"a" * 32
NEW = b"b" * 32
WRONG = b"c" * 32


def env(keyid=V2, raw=NEW):
    x = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return (f'A2A_HTTP_SIGNATURE_WORKER_KEY_ID="{keyid}"\n'
            'A2A_HTTP_SIGNATURE_WORKER_PRIVATE_KEY_JWK=\''
            + json.dumps({"kty": "OKP", "crv": "Ed25519", "x": x,
                          "d": "PRIVATE_MATERIAL_MUST_NOT_APPEAR"}) + "'\n")


class KeyringDriftTests(unittest.TestCase):
    def run_check(self, *, keyring=None, t1=None, local_text=None, local_status="ok", t2=None):
        output = io.StringIO()
        with patch.dict(os.environ, {"A2A_KEYRING_LOCAL_NODES": "alpha,alpha"}), \
                patch.object(checker, "load_keyring", return_value=keyring if keyring is not None else {V1: OLD, V2: NEW}), \
                patch.object(checker, "load_registry_t1", return_value=t1 if t1 is not None else {V2: NEW}), \
                patch.object(checker, "load_registry_t2", return_value=t2 or {}), \
                patch.object(checker, "read_worker_env", return_value=(local_status, env() if local_text is None else local_text)) as probe, \
                contextlib.redirect_stdout(output):
            rc = checker.main()
        text = output.getvalue()
        self.assertNotIn("PRIVATE_MATERIAL_MUST_NOT_APPEAR", text)
        self.assertNotIn("PRIVATE_KEY_JWK", text)
        self.assertEqual(probe.call_count, 1)
        result = json.loads(text)
        return rc, result, {r["keyid"]: r for r in result["rows"]}

    def test_rotation_preserves_old_key_without_false_alarm(self):
        rc, result, rows = self.run_check()
        self.assertEqual((rc, result["drift"]), (0, 0))
        self.assertEqual(rows[V1]["status"], "retained-key")
        self.assertEqual(rows[V2]["status"], "match")
        self.assertEqual(rows[V1]["local_keyid"], V2)

    def test_active_public_key_mismatch_is_still_drift(self):
        rc, result, rows = self.run_check(local_text=env(raw=WRONG))
        self.assertEqual((rc, result["drift"]), (1, 1))
        self.assertEqual(rows[V2]["status"], "DRIFT:keyring-vs-local")
        self.assertEqual(rows[V1]["status"], "retained-key")

    def test_same_keyid_registry_conflict_remains_drift(self):
        for registry in [{V2: WRONG}, {V1: WRONG, V2: NEW}]:
            with self.subTest(registry=registry):
                rc, result, _ = self.run_check(t1=registry)
                self.assertEqual((rc, result["drift"]), (1, 1))

    def test_second_broker_conflict_not_masked_by_first_match(self):
        rc, result, rows = self.run_check(t2={V2: WRONG})
        self.assertEqual((rc, result["drift"]), (1, 1))
        self.assertEqual(rows[V2]["status"], "DRIFT:keyring-vs-t2")

    def test_selected_key_missing_from_keyring_is_drift(self):
        for registry in [{V2: NEW}, {}]:
            with self.subTest(registry=registry):
                rc, result, rows = self.run_check(keyring={V1: OLD}, t1=registry)
                self.assertEqual((rc, result["drift"]), (1, 1))
                self.assertEqual(rows[V2]["status"], "DRIFT:active-not-in-keyring")

    def test_configured_v1_is_active_even_when_v2_exists(self):
        rc, _, rows = self.run_check(local_text=env(V1, OLD), t1={V1: OLD})
        self.assertEqual(rc, 0)
        self.assertEqual(rows[V1]["status"], "match")
        self.assertEqual(rows[V2]["status"], "retained-key")

    def test_registry_same_id_can_still_verify_nonselected_local_key(self):
        rc, _, rows = self.run_check(t1={V1: OLD, V2: NEW})
        self.assertEqual(rc, 0)
        self.assertEqual(rows[V1]["status"], "match")
        self.assertEqual(rows[V1]["local_probe"], "different-keyid")

    def test_missing_or_invalid_local_id_never_claims_historical(self):
        for text in [env().split("\n", 1)[1], env(OTHER), env(""), env("worker:alpha:"),
                     env() + f"A2A_HTTP_SIGNATURE_WORKER_KEY_ID={V1}\n"]:
            with self.subTest(text=text.splitlines()[0]):
                rc, _, rows = self.run_check(local_text=text)
                self.assertEqual(rc, 0)
                self.assertEqual(rows[V1]["status"], "unverifiable")
                self.assertEqual(rows[V1]["local_probe"], "local-keyid-unavailable")
                self.assertEqual(rows[V2]["status"], "match")  # Independent broker evidence.

    def test_unreadable_local_probe_uses_registry_without_guessing_history(self):
        for status in ["unreachable", "local-unreadable", "no-local-key"]:
            with self.subTest(status=status):
                _, _, rows = self.run_check(local_status=status)
                self.assertEqual(rows[V1]["status"], "unverifiable")
                self.assertEqual(rows[V2]["status"], "match")
                self.assertEqual(rows[V2]["local_probe"], status)

    def test_malformed_local_jwk_is_not_used_as_retirement_evidence(self):
        for text in [env().replace("{", "not-json{", 1), env(raw=b"short"),
                     f"A2A_HTTP_SIGNATURE_WORKER_KEY_ID={V2}\n"]:
            with self.subTest(text=text.splitlines()[0]):
                _, _, rows = self.run_check(local_text=text)
                self.assertEqual(rows[V1]["status"], "unverifiable")
                self.assertEqual(rows[V1]["local_probe"], "local-parse-failed")

    def test_unselected_registry_only_canary_keeps_info_status(self):
        rc, result, rows = self.run_check(t1={V2: NEW, OTHER: WRONG})
        self.assertEqual((rc, result["drift"]), (0, 0))
        self.assertEqual(rows[OTHER]["status"], "not-in-keyring")

    def test_unprobed_key_never_claims_retained(self):
        row = checker.compare_key(OTHER, {OTHER: OLD}, {}, None)
        self.assertEqual(row["status"], "unverifiable")

    def test_key_id_parses_shell_quotes_without_evaluating_shell(self):
        for value in [V2, f'"{V2}"', f"'{V2}'"]:
            self.assertEqual(checker.env_worker_keyid(f"A2A_HTTP_SIGNATURE_WORKER_KEY_ID={value}\n", "alpha"), V2)
        for value in [f"$(echo {V2})", f'"{V2}', V2 + " #comment"]:
            self.assertIsNone(checker.env_worker_keyid(f"A2A_HTTP_SIGNATURE_WORKER_KEY_ID={value}\n", "alpha"))


if __name__ == "__main__":
    unittest.main()
