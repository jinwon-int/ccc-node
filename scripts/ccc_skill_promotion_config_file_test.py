#!/usr/bin/env python3
"""#2239: the publisher config file (`~/.config/ccc-node/skill-promotion.env`).

Before this file existed every CCC_SKILL_PROMOTION_* knob lived only in the
process environment, so the nightly cron line, the 15-minute results cron and
an interactive `status` could each resolve different values with nothing
showing which. These tests pin the loader's contract:

- precedence: process env > file > default;
- only `CCC_SKILL_PROMOTION_*` keys are applied, everything else is counted
  (names never values) and dropped;
- a file that is not a regular owner-only file we own is ignored and reported
  in `status.config_file.error`, never raised — the sweep must not fail on it;
- a missing file is the quiet default;
- `status` shows the `config_file` block and the effective knobs.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "promotion_config_file_test", Path(__file__).with_name("ccc-skill-promotion.py")
)
assert SPEC is not None and SPEC.loader is not None
promotion = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = promotion
SPEC.loader.exec_module(promotion)


class ConfigFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        os.chmod(self.home, 0o700)
        self.path = self.home / ".config" / "ccc-node" / "skill-promotion.env"
        self.path.parent.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, text: str, mode: int = 0o600, path: Path | None = None) -> Path:
        target = path or self.path
        target.write_text(text, encoding="utf-8")
        target.chmod(mode)
        return target

    def env(self, **extra: str) -> dict[str, str]:
        base = {"HOME": str(self.home), "CCC_NODE": "testnode"}
        base.update(extra)
        return base

    def test_missing_file_is_the_quiet_default(self) -> None:
        cfg = promotion._config(self.env())
        self.assertIsNotNone(cfg.config_file)
        assert cfg.config_file is not None
        self.assertFalse(cfg.config_file.present)
        self.assertIsNone(cfg.config_file.error)
        self.assertEqual(cfg.config_file.path, str(self.path))
        self.assertEqual(cfg.collect_window, 32)

    def test_file_fills_gaps_and_process_env_wins(self) -> None:
        self.write(
            "# publisher knobs\n"
            "export CCC_SKILL_PROMOTION_COLLECT_WINDOW=\"7\"\n"
            "CCC_SKILL_PROMOTION_REVISE_DAILY_CAP='5'\n"
            "CCC_SKILL_PROMOTION_MAX_PRS_PER_RUN=2   # trailing comment\n"
        )
        cfg = promotion._config(self.env(CCC_SKILL_PROMOTION_COLLECT_WINDOW="9"))
        self.assertEqual(cfg.collect_window, 9, "process env wins over the file")
        self.assertEqual(cfg.revise_daily_cap, 5, "file fills an unset knob (quotes stripped)")
        self.assertEqual(cfg.max_prs, 2, "unquoted value ends at the trailing comment")
        assert cfg.config_file is not None
        self.assertTrue(cfg.config_file.present)
        self.assertIsNone(cfg.config_file.error)
        self.assertEqual(
            cfg.config_file.applied_keys,
            ("CCC_SKILL_PROMOTION_REVISE_DAILY_CAP", "CCC_SKILL_PROMOTION_MAX_PRS_PER_RUN"),
        )
        self.assertEqual(cfg.config_file.overridden_keys, ("CCC_SKILL_PROMOTION_COLLECT_WINDOW",))

    def test_non_whitelisted_keys_are_counted_and_never_applied(self) -> None:
        self.write(
            "A2A_EDGE_SECRET=should-never-apply\n"
            "HOME=/elsewhere\n"
            "CCC_STATE_DIR=/elsewhere/state\n"
            "CCC_SKILL_PROMOTION_ENV_FILE=/elsewhere/other.env\n"
            "CCC_SKILL_PROMOTION_OUTBOX_STALE_HOURS=24\n"
            "this is not an assignment\n"
        )
        env = self.env()
        report = promotion._apply_config_file(env, self.home)
        self.assertEqual(report.ignored_keys, 4)
        self.assertEqual(report.malformed_lines, 1)
        self.assertEqual(report.applied_keys, ("CCC_SKILL_PROMOTION_OUTBOX_STALE_HOURS",))
        self.assertNotIn("A2A_EDGE_SECRET", env)
        self.assertEqual(env["HOME"], str(self.home))
        self.assertNotIn("CCC_STATE_DIR", env)
        self.assertNotIn("CCC_SKILL_PROMOTION_ENV_FILE", env)
        summary = report.summary()
        self.assertNotIn("should-never-apply", repr(summary))
        self.assertEqual(summary["ignored_keys"], 4)
        cfg = promotion._config(self.env())
        self.assertEqual(cfg.outbox_stale_hours, 24)
        self.assertEqual(cfg.state_dir, (self.home / ".claude" / "state").absolute())

    def test_group_or_other_readable_file_is_ignored_not_fatal(self) -> None:
        self.write("CCC_SKILL_PROMOTION_COLLECT_WINDOW=7\n", mode=0o644)
        cfg = promotion._config(self.env())
        assert cfg.config_file is not None
        self.assertTrue(cfg.config_file.present)
        self.assertEqual(cfg.config_file.error, "config_file_unsafe")
        self.assertEqual(cfg.config_file.applied_keys, ())
        self.assertEqual(cfg.collect_window, 32, "nothing from a 0644 file is applied")
        self.write("CCC_SKILL_PROMOTION_COLLECT_WINDOW=7\n", mode=0o640)
        cfg = promotion._config(self.env())
        assert cfg.config_file is not None
        self.assertEqual(cfg.config_file.error, "config_file_unsafe", "a group bit alone is enough")

    def test_symlink_directory_and_oversize_are_ignored(self) -> None:
        real = self.write("CCC_SKILL_PROMOTION_COLLECT_WINDOW=7\n", path=self.home / "real.env")
        link = self.home / "link.env"
        link.symlink_to(real)
        report = promotion._apply_config_file(self.env(CCC_SKILL_PROMOTION_ENV_FILE=str(link)), self.home)
        self.assertEqual(report.error, "config_file_unsafe")
        directory = self.home / "adir"
        directory.mkdir(mode=0o700)
        report = promotion._apply_config_file(self.env(CCC_SKILL_PROMOTION_ENV_FILE=str(directory)), self.home)
        self.assertEqual(report.error, "config_file_unsafe")
        big = self.write("#" * (promotion._CONFIG_FILE_MAX_BYTES + 1) + "\n", path=self.home / "big.env")
        report = promotion._apply_config_file(self.env(CCC_SKILL_PROMOTION_ENV_FILE=str(big)), self.home)
        self.assertEqual(report.error, "config_file_too_large")
        self.assertEqual(report.applied_keys, ())

    def test_path_override_env(self) -> None:
        other = self.write("CCC_SKILL_PROMOTION_REVISE_ROUNDS=1\n", path=self.home / "other.env")
        cfg = promotion._config(self.env(CCC_SKILL_PROMOTION_ENV_FILE=str(other)))
        assert cfg.config_file is not None
        self.assertEqual(cfg.config_file.path, str(other))
        self.assertEqual(cfg.revise_round_limit, 1)

    def test_repeated_key_later_line_wins_like_sourcing(self) -> None:
        self.write(
            "CCC_SKILL_PROMOTION_COLLECT_WINDOW=7\n"
            "CCC_SKILL_PROMOTION_COLLECT_WINDOW=8\n"
        )
        cfg = promotion._config(self.env())
        self.assertEqual(cfg.collect_window, 8)
        assert cfg.config_file is not None
        self.assertEqual(cfg.config_file.applied_keys, ("CCC_SKILL_PROMOTION_COLLECT_WINDOW",))

    def test_file_value_still_passes_through_bounded_validation(self) -> None:
        # bounded_int_env falls back to the default for garbage, exactly as it
        # would for a garbage process-env value; the file gains no new power.
        self.write("CCC_SKILL_PROMOTION_COLLECT_WINDOW=not-a-number\n")
        cfg = promotion._config(self.env())
        self.assertEqual(cfg.collect_window, 32)

    def test_status_shows_config_file_and_effective_knobs_when_disabled(self) -> None:
        self.write("CCC_SKILL_PROMOTION_REVISE_DAILY_CAP=6\nA2A_EDGE_SECRET=x\n")
        cfg = promotion._config(self.env())
        self.assertFalse(cfg.enabled)
        status = promotion._status(cfg)
        self.assertEqual(status["enabled"], False)
        block = status["config_file"]
        assert isinstance(block, dict)
        self.assertEqual(block["path"], str(self.path))
        self.assertTrue(block["present"])
        self.assertIsNone(block["error"])
        self.assertEqual(block["applied_keys"], ["CCC_SKILL_PROMOTION_REVISE_DAILY_CAP"])
        self.assertEqual(block["ignored_keys"], 1)
        effective = status["effective"]
        assert isinstance(effective, dict)
        self.assertEqual(effective["revise_daily_cap"], 6)
        for key in (
            "max_prs_per_run", "revise_daily_cap", "revise_round_limit", "collect_window",
            "outbox_stale_hours", "dispatch_ci_wait_sec", "remote_brokers",
        ):
            self.assertIn(key, effective)
        self.assertEqual(effective["remote_brokers"], 0)
        self.assertNotIn("x", [v for v in block.values() if isinstance(v, str)])

    def test_status_shows_the_same_values_a_cron_run_resolves_when_enabled(self) -> None:
        self.write(
            "CCC_SKILL_PROMOTION_MAX_PRS_PER_RUN=3\n"
            "CCC_SKILL_PROMOTION_DISPATCH_CI_WAIT_SEC=900\n"
            "CCC_SKILL_PROMOTION_OUTBOX_STALE_HOURS=48\n"
        )
        cfg = promotion._config(self.env(CCC_SKILL_PROMOTION_ENABLED="1"))
        self.assertTrue(cfg.enabled)
        with patch.object(promotion, "_discover", return_value=([], [], [])), \
                patch.object(promotion, "_outbox_summary", return_value={}):
            status = promotion._status(cfg)
        self.assertEqual(status["max_prs_per_run"], 3)
        effective = status["effective"]
        assert isinstance(effective, dict)
        self.assertEqual(effective["max_prs_per_run"], 3)
        self.assertEqual(effective["dispatch_ci_wait_sec"], 900)
        self.assertEqual(effective["outbox_stale_hours"], 48)
        self.assertEqual(effective["collect_window"], 32)
        block = status["config_file"]
        assert isinstance(block, dict)
        self.assertEqual(
            block["applied_keys"],
            [
                "CCC_SKILL_PROMOTION_MAX_PRS_PER_RUN",
                "CCC_SKILL_PROMOTION_DISPATCH_CI_WAIT_SEC",
                "CCC_SKILL_PROMOTION_OUTBOX_STALE_HOURS",
            ],
        )

    def test_cap_accepts_up_to_eight_and_rejects_out_of_range(self) -> None:
        # 2026-10-22: a 3-PR nightly cap left 24 candidates queued while review
        # and arbitration cleared 14 intake PRs in a day; the old 1-3 bound
        # silently fell back to 1 for any larger value.
        self.write("CCC_SKILL_PROMOTION_MAX_PRS_PER_RUN=6\n")
        self.assertEqual(promotion._config(self.env(CCC_SKILL_PROMOTION_ENABLED="1")).max_prs, 6)
        self.write("CCC_SKILL_PROMOTION_MAX_PRS_PER_RUN=9\n")
        self.assertEqual(promotion._config(self.env(CCC_SKILL_PROMOTION_ENABLED="1")).max_prs, 1)

    def test_unsafe_file_never_fails_main(self) -> None:
        self.write("CCC_SKILL_PROMOTION_COLLECT_WINDOW=7\n", mode=0o644)
        with patch.dict(os.environ, self.env(), clear=True):
            captured: list[str] = []
            with patch("builtins.print", side_effect=lambda line, *a, **k: captured.append(str(line))):
                rc = promotion.main(["status"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(captured), 1)
        self.assertIn('"error": "config_file_unsafe"', captured[0].replace('":"', '": "'))


class AutoApproveConfigTests(unittest.TestCase):
    """#2239 L3: `auto_approve_promote` tri-state + relay knobs + status block."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        os.chmod(self.home, 0o700)
        self.state = self.home / ".claude" / "state"
        self.state.mkdir(parents=True, mode=0o700)
        self.flag = self.state / "skill-promotion.auto-approve"
        self.script = self.home / "approve-via-relay.sh"
        self.script.write_text("#!/bin/sh\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def env(self, **extra: str) -> dict[str, str]:
        base = {"HOME": str(self.home), "CCC_NODE": "testnode",
                "CCC_SKILL_PROMOTION_APPROVE_CMD": str(self.script)}
        base.update(extra)
        return base

    def test_default_is_off(self) -> None:
        cfg = promotion._config(self.env())
        self.assertEqual(cfg.auto_approve_mode, "off")
        self.assertEqual(cfg.approve_profile, "seoseo-ai")
        self.assertEqual(cfg.approve_ssh_target, "")

    def test_env_tri_state(self) -> None:
        for raw, mode in (("true", "true"), ("1", "true"), ("dry-run", "dry-run"),
                          ("DRY-RUN", "dry-run"), ("off", "off"), ("false", "off"), ("0", "off")):
            with self.subTest(raw=raw):
                cfg = promotion._config(self.env(CCC_SKILL_PROMOTION_AUTO_APPROVE=raw))
                self.assertEqual(cfg.auto_approve_mode, mode)
        with self.assertRaises(promotion.PromotionError) as caught:
            promotion._config(self.env(CCC_SKILL_PROMOTION_AUTO_APPROVE="maybe"))
        self.assertEqual(caught.exception.code, "auto_approve_invalid")

    def test_owner_flag_file(self) -> None:
        for content, mode in (("true\n", "true"), ("dry-run\n", "dry-run"), ("off\n", "off"),
                              ("garbage\n", "off")):
            with self.subTest(content=content):
                self.flag.write_text(content, encoding="utf-8")
                self.flag.chmod(0o600)
                self.assertEqual(promotion._config(self.env()).auto_approve_mode, mode)
        self.flag.write_text("true\n", encoding="utf-8")
        self.flag.chmod(0o644)
        self.assertEqual(promotion._config(self.env()).auto_approve_mode, "off",
                         "a flag file others can write is not the owner's approval")
        self.flag.chmod(0o600)
        self.assertEqual(
            promotion._config(self.env(CCC_SKILL_PROMOTION_AUTO_APPROVE="off")).auto_approve_mode,
            "off", "process env wins over the flag file")

    def test_config_file_can_set_the_knobs(self) -> None:
        path = self.home / ".config" / "ccc-node" / "skill-promotion.env"
        path.parent.mkdir(parents=True)
        path.write_text(
            "CCC_SKILL_PROMOTION_AUTO_APPROVE=dry-run\n"
            "CCC_SKILL_PROMOTION_APPROVE_PROFILE=jinon86\n"
            "CCC_SKILL_PROMOTION_APPROVE_SSH_TARGET=relay-node\n", encoding="utf-8")
        path.chmod(0o600)
        cfg = promotion._config(self.env())
        self.assertEqual((cfg.auto_approve_mode, cfg.approve_profile, cfg.approve_ssh_target),
                         ("dry-run", "jinon86", "relay-node"))

    def test_invalid_profile_fails(self) -> None:
        with self.assertRaises(promotion.PromotionError) as caught:
            promotion._config(self.env(CCC_SKILL_PROMOTION_APPROVE_PROFILE="someone"))
        self.assertEqual(caught.exception.code, "approve_profile_invalid")

    def test_approve_cmd_resolution(self) -> None:
        cfg = promotion._config(self.env())
        self.assertEqual(cfg.approve_cmd, ("bash", str(self.script)))
        self.assertTrue(cfg.approve_cmd_present)
        cfg = promotion._config(self.env(CCC_SKILL_PROMOTION_APPROVE_CMD=str(self.home / "nope.sh")))
        self.assertFalse(cfg.approve_cmd_present)
        env = self.env()
        del env["CCC_SKILL_PROMOTION_APPROVE_CMD"]
        codex = self.home / "codex-home"
        script = codex.joinpath(*promotion._APPROVE_SCRIPT_REL)
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        cfg = promotion._config({**env, "CODEX_HOME": str(codex)})
        self.assertEqual(cfg.approve_cmd, ("bash", str(script)), "managed codex skill copy first")
        self.assertTrue(cfg.approve_cmd_present)

    def test_status_block_when_disabled_and_enabled(self) -> None:
        cfg = promotion._config(self.env(CCC_SKILL_PROMOTION_AUTO_APPROVE="true"))
        status = promotion._status(cfg)
        expected = {"mode": "true", "cmd_present": True, "profile": "seoseo-ai",
                    "ssh_target_set": False, "blocker": "approve_ssh_target_unset"}
        self.assertEqual(status["auto_approve"], expected)
        effective = status["effective"]
        assert isinstance(effective, dict)
        self.assertEqual(effective["auto_approve"], expected)
        cfg = promotion._config(self.env(
            CCC_SKILL_PROMOTION_ENABLED="1", CCC_SKILL_PROMOTION_AUTO_APPROVE="true",
            CCC_SKILL_PROMOTION_APPROVE_SSH_TARGET="relay-node"))
        with patch.object(promotion, "_discover", return_value=([], [], [])), \
                patch.object(promotion, "_outbox_summary", return_value={}):
            status = promotion._status(cfg)
        block = status["auto_approve"]
        assert isinstance(block, dict)
        self.assertEqual((block["ssh_target_set"], block["blocker"]), (True, None))
        self.assertNotIn("relay-node", json.dumps(status), "presence only, never the target")
        cfg = promotion._config(self.env(CCC_SKILL_PROMOTION_APPROVE_CMD=str(self.home / "nope.sh"),
                                         CCC_SKILL_PROMOTION_AUTO_APPROVE="dry-run",
                                         CCC_SKILL_PROMOTION_APPROVE_SSH_TARGET="relay-node"))
        block = promotion._status(cfg)["auto_approve"]
        assert isinstance(block, dict)
        self.assertEqual((block["cmd_present"], block["blocker"]), (False, "approve_cmd_missing"))
        off = promotion._status(promotion._config(self.env()))["auto_approve"]
        assert isinstance(off, dict)
        self.assertEqual((off["mode"], off["blocker"]), ("off", None))


if __name__ == "__main__":
    unittest.main()
