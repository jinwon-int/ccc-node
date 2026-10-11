#!/usr/bin/env python3
"""Hermetic verdict tests for doctor's skill-promotion last-collect check (#2232).

A nightly collect that lost `promotion.lock` used to report `ok: true,
status: "locked"`: the whole cycle was skipped and nothing anywhere failed.
The sweep persists every collect result to `skill-promotion/last-collect.json`
(#2203); these tests pin that doctor reads it — absence is not drift, a failed
or lock-skipped cycle is 경고 with an actionable hint, and nothing escalates
past 경고 or echoes untrusted values.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import os
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ccc_doctor import Doctor  # noqa: E402

ITEM = "skill-promotion last collect"


class SkillPromotionLastCollectCheck(unittest.TestCase):
    def run_check(self, payload: object | None, *, raw: str | None = None,
                  age_hours: float = 1, symlink: bool = False) -> Doctor:
        with TemporaryDirectory() as temp:
            claude_dir = Path(temp) / ".claude"
            promo = claude_dir / "state" / "skill-promotion"
            promo.mkdir(parents=True)
            path = promo / "last-collect.json"
            if symlink:
                target = Path(temp) / "elsewhere.json"
                target.write_text('{"ok": false}', encoding="utf-8")
                path.symlink_to(target)
            elif raw is not None or payload is not None:
                path.write_text(raw if raw is not None else json.dumps(payload), encoding="utf-8")
                stamp = time.time() - age_hours * 3600
                os.utime(path, (stamp, stamp))
            doctor = Doctor(Path.cwd(), claude_dir, "settings")
            with patch.dict("os.environ", {}, clear=True):
                doctor.check_skill_promotion_last_collect()
            return doctor

    def assert_row(self, doctor: Doctor, klass: str, fragment: str):
        row = doctor.rows[-1]
        self.assertEqual(row.item, ITEM)
        self.assertEqual(row.klass, klass, f"status was: {row.status}")
        self.assertIn(fragment, row.status)
        return row

    # --- absence is not drift ------------------------------------------------

    def test_absent_file_is_normal(self) -> None:
        self.assert_row(self.run_check(None), "정상", "last-collect=absent")

    def test_symlink_is_treated_as_absent(self) -> None:
        self.assert_row(self.run_check(None, symlink=True), "정상", "last-collect=absent")

    # --- a good cycle --------------------------------------------------------

    def test_ok_cycle_is_normal(self) -> None:
        doctor = self.run_check({"ok": True, "mode": "collect", "published": [{}, {}], "errors": []})
        row = self.assert_row(doctor, "정상", "published=2")
        self.assertIn("age=1h", row.status)

    # --- the field case: lock contention -------------------------------------

    def test_lock_timeout_warns_with_lock_hint(self) -> None:
        doctor = self.run_check({"ok": False, "mode": "collect", "status": "locked",
                                 "code": "lock_timeout", "lock_wait_sec": 120,
                                 "published": [], "errors": []})
        row = self.assert_row(doctor, "경고", "code=lock_timeout")
        self.assertIn("CCC_SKILL_PROMOTION_LOCK_WAIT_SEC", row.action)

    def test_legacy_ok_true_locked_still_warns(self) -> None:
        """The pre-#2232 shape (single-try caller) skipped the cycle too."""
        doctor = self.run_check({"ok": True, "mode": "collect", "status": "locked",
                                 "published": [], "errors": []})
        self.assert_row(doctor, "경고", "status=locked (cycle skipped)")

    # --- other failures ------------------------------------------------------

    def test_failure_with_errors_warns_with_generic_hint(self) -> None:
        doctor = self.run_check({"ok": False, "errors": [{"source": "a", "code": "x"}] * 3})
        row = self.assert_row(doctor, "경고", "ok=false; code=none; errors=3")
        self.assertIn("rerun collect", row.action)

    def test_untrusted_code_is_not_echoed(self) -> None:
        doctor = self.run_check({"ok": False, "code": "Weird $(id)"})
        row = self.assert_row(doctor, "경고", "code=none")
        self.assertNotIn("Weird", row.status)

    def test_malformed_json_warns(self) -> None:
        self.assert_row(self.run_check(None, raw="{not json"), "경고", "last-collect=malformed")

    def test_non_object_json_warns(self) -> None:
        self.assert_row(self.run_check(["ok"]), "경고", "last-collect=malformed")

    def test_never_escalates_past_warning(self) -> None:
        for payload in ({"ok": False}, {"ok": True, "status": "locked"}, {"ok": "yes"}):
            doctor = self.run_check(payload)
            self.assertIn(doctor.rows[-1].klass, {"정상", "경고"})


if __name__ == "__main__":
    unittest.main()
