#!/usr/bin/env python3
"""Unit tests for scripts/a2a-broker-worker-watch.py (#2086).

Hermetic: the broker is a fixture-backed fake fetcher (no network, no curl)
and time is injected. scripts/a2a-broker-worker-watch.test.sh runs this file
and adds the curl-stub end-to-end checks (secret transport, exit codes).
"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("a2a_broker_worker_watch", HERE / "a2a-broker-worker-watch.py")
assert _spec and _spec.loader
watch: Any = importlib.util.module_from_spec(_spec)
sys.modules["a2a_broker_worker_watch"] = watch
_spec.loader.exec_module(watch)

T0 = 1_790_000_000.0
MIN = 60.0
HOUR = 3600.0
TOKENS = ("DOWN", "UNREACHABLE", "DRIFT", "BOOTPATH", "DUALDOMAIN", "NONCANONICAL",
          "DEGRADED", "UNVERIFIED")


def iso(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def payload(now: float, **nodes: Any) -> dict[str, Any]:
    """nodes: name -> "online" | "stale" | ("stale", last_seen_age_seconds)."""
    items = []
    for name, spec in nodes.items():
        status, age = (spec, 30.0) if isinstance(spec, str) else spec
        items.append({"nodeId": name, "status": status, "lastSeenAt": iso(now - age),
                      "capabilities": {"secretish": "PAYLOAD-SENTINEL"}})
    return {"items": items}


class Harness:
    """Drives watch.run() over successive ticks against one fake broker."""

    def __init__(self, tmp: str, *extra: str, env: dict[str, str] | None = None) -> None:
        self.state = os.path.join(tmp, "state", "watch.json")
        argv = ["--broker", "team9=http://127.0.0.1:1", "--edge-env-file", "/nonexistent",
                "--state-file", self.state, *extra]
        self.cfg = watch.parse_settings(argv, env or {})
        self.next: Any = None
        self.lines: list[str] = []
        self.warnings: list[str] = []

    def fetcher(self, broker: Any, timeout: int) -> Any:
        nxt = self.next
        if isinstance(nxt, dict):
            workers = watch.parse_workers(nxt.get("body"))
            return watch.Fetch(True, workers=workers or [], uptime=nxt.get("uptime"),
                               draining=bool(nxt.get("draining")))
        return nxt

    def tick(self, now: float, nxt: Any) -> int:
        self.next = nxt
        self.lines = []
        self.warnings = []
        return watch.run(self.cfg, self.fetcher, now, self.lines.append, self.warnings.append)

    def ok(self, now: float, uptime: float | None = 99 * HOUR, **nodes: Any) -> int:
        return self.tick(now, {"body": payload(now, **nodes), "uptime": uptime})

    def paged(self) -> list[str]:
        return [ln for ln in self.lines if ln.split(" ", 1)[0] in TOKENS]

    def state_doc(self) -> dict[str, Any]:
        return json.loads(Path(self.state).read_text(encoding="utf-8"))


class BaseCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def harness(self, *extra: str, env: dict[str, str] | None = None) -> Harness:
        return Harness(self.tmp, *extra, env=env)


class ThresholdAndRealert(BaseCase):
    def test_stale_pages_only_after_threshold_then_staged_realert(self) -> None:
        h = self.harness()
        self.assertEqual(h.ok(T0, alpha="online", beta="online"), 0)
        self.assertEqual(h.ok(T0 + 5 * MIN, alpha="stale", beta="online"), 0)
        self.assertIn("PENDING alpha source=broker:team9 reason=worker-stale", "\n".join(h.lines))
        self.assertEqual(h.ok(T0 + 15 * MIN, alpha="stale", beta="online"), 0, h.lines)
        rc = h.ok(T0 + 20 * MIN, alpha=("stale", 16 * MIN), beta="online")
        self.assertEqual(rc, 1)
        self.assertEqual(h.paged(), ["DOWN alpha source=broker:team9 reason=worker-stale age=16m"])
        # Next 5-minute run: still down, but not due again -> quiet (exit 0).
        self.assertEqual(h.ok(T0 + 25 * MIN, alpha="stale", beta="online"), 0)
        self.assertEqual(h.paged(), [])
        self.assertTrue(any(ln.startswith("ONGOING alpha ") for ln in h.lines))
        # Stage 1: one hour after the first page.
        self.assertEqual(h.ok(T0 + 80 * MIN, alpha="stale", beta="online"), 1)
        self.assertEqual(h.ok(T0 + 85 * MIN, alpha="stale", beta="online"), 0)
        # Stage 2 repeats every 6h.
        self.assertEqual(h.ok(T0 + 80 * MIN + 5 * HOUR, alpha="stale", beta="online"), 0)
        self.assertEqual(h.ok(T0 + 80 * MIN + 6 * HOUR, alpha="stale", beta="online"), 1)
        self.assertEqual(h.ok(T0 + 80 * MIN + 12 * HOUR, alpha="stale", beta="online"), 1)

    def test_age_uses_broker_last_seen_when_older(self) -> None:
        h = self.harness("--threshold", "0s")
        self.assertEqual(h.ok(T0, alpha=("stale", 10 * HOUR + 2 * MIN)), 1)
        self.assertEqual(h.paged(), ["DOWN alpha source=broker:team9 reason=worker-stale age=10h02m"])

    def test_flapping_node_restarts_its_clock(self) -> None:
        h = self.harness()
        h.ok(T0, alpha="stale")
        h.ok(T0 + 10 * MIN, alpha="online")
        self.assertEqual(h.ok(T0 + 20 * MIN, alpha="stale"), 0)
        self.assertEqual(h.ok(T0 + 30 * MIN, alpha="stale"), 0)
        self.assertEqual(h.ok(T0 + 35 * MIN, alpha="stale"), 1)

    def test_missing_node_seen_online_before_pages(self) -> None:
        h = self.harness("--threshold", "10m")
        h.ok(T0, alpha="online", beta="online")
        h.ok(T0 + 5 * MIN, beta="online")
        self.assertEqual(h.ok(T0 + 15 * MIN, beta="online"), 1)
        self.assertEqual(h.paged(), ["DOWN alpha source=broker:team9 reason=worker-missing age=10m"])


class RecoveryAndExclusion(BaseCase):
    def _down_then_up(self, h: Harness) -> int:
        h.ok(T0, alpha="stale", beta="online")
        h.ok(T0 + 20 * MIN, alpha="stale", beta="online")
        return h.ok(T0 + 25 * MIN, alpha="online", beta="online")

    def test_recovery_is_quiet_by_default(self) -> None:
        h = self.harness()
        self.assertEqual(self._down_then_up(h), 0)
        self.assertIn("RECOVERED alpha source=broker:team9 down=25m", h.lines)
        self.assertEqual(h.state_doc()["brokers"]["team9"]["nodes"]["alpha"], {"seenOnline": True})

    def test_recovery_pages_when_opted_in(self) -> None:
        h = self.harness("--report-recovery")
        self.assertEqual(self._down_then_up(h), 1)
        self.assertIn("RECOVERED alpha source=broker:team9 down=25m", h.lines)

    def test_unpaged_recovery_is_silent(self) -> None:
        h = self.harness("--report-recovery")
        h.ok(T0, alpha="stale")
        self.assertEqual(h.ok(T0 + 5 * MIN, alpha="online"), 0)
        self.assertFalse(any(ln.startswith("RECOVERED") for ln in h.lines))

    def test_exclusion_from_cli_and_env(self) -> None:
        h = self.harness("--exclude", "alpha", env={"A2A_WORKER_WATCH_EXCLUDE": "gamma, delta"})
        self.assertEqual(h.cfg.exclude, frozenset({"alpha", "gamma", "delta"}))
        h.ok(T0, alpha="stale", gamma="stale", beta="online")
        self.assertEqual(h.ok(T0 + HOUR, alpha="stale", gamma="stale", beta="online"), 0)
        self.assertEqual(h.paged(), [])
        self.assertIn("excluded=2", h.lines[-1])

    def test_abandoned_identity_ignored_unless_seen_online(self) -> None:
        h = self.harness()
        h.ok(T0, canary=("stale", 30 * 86400.0), beta="online")
        self.assertEqual(h.ok(T0 + HOUR, canary=("stale", 30 * 86400.0), beta="online"), 0)
        self.assertIn("ignored=1", h.lines[-1])
        h2 = Harness(os.path.join(self.tmp, "b"))
        h2.ok(T0, alpha="online")
        h2.ok(T0 + 5 * MIN, alpha=("stale", 30 * 86400.0))
        self.assertEqual(h2.ok(T0 + 25 * MIN, alpha=("stale", 30 * 86400.0)), 1)


class BrokerFailures(BaseCase):
    def test_unreachable_broker_is_one_finding_and_freezes_nodes(self) -> None:
        h = self.harness()
        h.ok(T0, alpha="online", beta="stale")
        before = h.state_doc()["brokers"]["team9"]["nodes"]
        down = watch.Fetch(False, kind="unreachable", reason="dns")
        self.assertEqual(h.tick(T0 + 5 * MIN, down), 0)
        self.assertIn("PENDING broker:team9 reason=dns age=0m runs=1", h.lines)
        self.assertEqual(h.tick(T0 + 10 * MIN, down), 1)
        self.assertEqual(h.paged(), ["UNREACHABLE broker:team9 reason=dns age=5m runs=2"])
        self.assertEqual(h.state_doc()["brokers"]["team9"]["nodes"], before)
        self.assertEqual(h.tick(T0 + 15 * MIN, down), 0)  # staged re-alert
        self.assertEqual(h.tick(T0 + 70 * MIN, down), 1)

    def test_auth_rejection_is_degraded(self) -> None:
        h = self.harness("--broker-fail-runs", "1")
        rc = h.tick(T0, watch.Fetch(False, kind="degraded", reason="auth-rejected"))
        self.assertEqual(rc, 1)
        self.assertEqual(h.paged(), ["DEGRADED broker:team9 reason=auth-rejected age=0m runs=1"])

    def test_config_failure_exits_2(self) -> None:
        h = self.harness()
        rc = h.tick(T0, watch.Fetch(False, kind="config", reason="secret-missing"))
        self.assertEqual(rc, 2)
        self.assertEqual(h.paged(), ["UNVERIFIED broker:team9 reason=secret-missing"])

    def test_recovery_from_outage_grants_grace(self) -> None:
        h = self.harness("--report-recovery")
        h.ok(T0, alpha="online", beta="online")
        down = watch.Fetch(False, kind="unreachable", reason="timeout")
        h.tick(T0 + 5 * MIN, down)
        h.tick(T0 + 10 * MIN, down)
        self.assertEqual(h.ok(T0 + 15 * MIN, alpha="stale", beta="online"), 1)
        self.assertIn("RECOVERED broker:team9 down=10m", h.lines)
        # alpha went stale inside the grace window: clock starts at its end.
        self.assertEqual(h.ok(T0 + 35 * MIN, alpha="stale", beta="online"), 0)
        self.assertEqual(h.ok(T0 + 40 * MIN, alpha="stale", beta="online"), 1)


class RestartGraceAndMass(BaseCase):
    NODES = ("a1", "a2", "a3", "a4", "a5")

    def _all(self, h: Harness, now: float, status: str, uptime: float = 99 * HOUR) -> int:
        return h.ok(now, uptime=uptime, **{n: status for n in self.NODES})

    def test_livez_restart_delays_the_clock(self) -> None:
        h = self.harness()
        h.ok(T0, alpha="online", beta="online")
        self.assertEqual(h.ok(T0 + 5 * MIN, uptime=60, alpha="stale", beta="online"), 0)
        self.assertEqual(h.ok(T0 + 20 * MIN, alpha="stale", beta="online"), 0)
        self.assertEqual(h.ok(T0 + 30 * MIN, alpha="stale", beta="online"), 1)

    def test_draining_opens_grace(self) -> None:
        h = self.harness()
        h.ok(T0, alpha="online")
        h.tick(T0 + 5 * MIN, {"body": payload(T0, alpha="stale"), "draining": True})
        self.assertEqual(h.ok(T0 + 20 * MIN, alpha="stale"), 0)
        self.assertEqual(h.ok(T0 + 30 * MIN, alpha="stale"), 1)

    def test_mass_flip_grace_then_single_broker_line(self) -> None:
        h = self.harness()
        self._all(h, T0, "online")
        self.assertEqual(self._all(h, T0 + 5 * MIN, "stale"), 0)
        self.assertEqual(self._all(h, T0 + 20 * MIN, "stale"), 0)  # inside grace + threshold
        self.assertEqual(self._all(h, T0 + 30 * MIN, "stale"), 1)
        self.assertEqual(len(h.paged()), 1, h.paged())
        self.assertTrue(h.paged()[0].startswith(
            "DEGRADED broker:team9 reason=mass-stale stale=5/5 nodes=a1,a2,a3,a4,a5 age="))
        self.assertEqual(self._all(h, T0 + 35 * MIN, "stale"), 0)
        # Partial heal: the remaining node already counts as paged -> no burst.
        rc = h.ok(T0 + 40 * MIN, a1="online", a2="online", a3="online", a4="online", a5="stale")
        self.assertEqual(rc, 0, h.lines)

    def test_quick_mass_flip_heals_without_page(self) -> None:
        h = self.harness()
        self._all(h, T0, "online")
        self._all(h, T0 + 5 * MIN, "stale")
        self.assertEqual(self._all(h, T0 + 10 * MIN, "online"), 0)
        self.assertEqual(self._all(h, T0 + 30 * MIN, "online"), 0)


class StateAndSafety(BaseCase):
    def test_state_is_owner_only_and_atomic(self) -> None:
        h = self.harness()
        h.ok(T0, alpha="online")
        mode = stat.S_IMODE(os.stat(h.state).st_mode)
        self.assertEqual(mode, 0o600)
        leftovers = [p for p in os.listdir(os.path.dirname(h.state)) if p.startswith(".a2a-worker-watch.")]
        self.assertEqual(leftovers, [])

    def test_corrupt_state_resets_with_warning(self) -> None:
        h = self.harness()
        os.makedirs(os.path.dirname(h.state), exist_ok=True)
        Path(h.state).write_text("{not json", encoding="utf-8")
        self.assertEqual(h.ok(T0, alpha="online"), 0)
        self.assertEqual(h.warnings, ["WARN state-reset reason=corrupt"])
        Path(h.state).write_text('{"version": 99, "brokers": {}}', encoding="utf-8")
        h.ok(T0 + MIN, alpha="online")
        self.assertEqual(h.warnings, ["WARN state-reset reason=bad-shape"])
        self.assertEqual(h.state_doc()["version"], 1)

    def test_hostile_node_id_cannot_forge_a_line(self) -> None:
        h = self.harness("--threshold", "0s")
        evil = "x\nDOWN forged reason=evil"
        rc = h.tick(T0, {"body": {"items": [{"nodeId": evil, "status": "stale\nDRIFT y"}]}})
        self.assertEqual(rc, 1)
        self.assertEqual(len(h.paged()), 1)
        line = h.paged()[0]
        self.assertRegex(line, r"^DOWN node-[0-9a-f]{8} source=broker:team9 reason=worker-unknown age=0m$")

    def test_output_never_carries_payload_fields(self) -> None:
        h = self.harness("--threshold", "0s")
        h.ok(T0, alpha="stale", beta="online")
        self.assertNotIn("PAYLOAD-SENTINEL", "\n".join(h.lines + h.warnings))

    def test_bad_payload_is_degraded(self) -> None:
        self.assertIsNone(watch.parse_workers({"nope": 1}))
        self.assertIsNone(watch.parse_workers([1, 2]))
        workers = watch.parse_workers({"items": [
            {"nodeId": "a", "status": "stale"}, {"nodeId": "a", "status": "online"}, "junk", {}]})
        self.assertEqual([(w.node, w.online) for w in workers], [("a", True)])

    def test_summary_line_is_not_a_fleet_token(self) -> None:
        h = self.harness()
        h.ok(T0, alpha="online")
        self.assertTrue(h.lines[-1].startswith("SUMMARY brokers=1/1 workers=1 online=1 paged=0"))


class Usage(unittest.TestCase):
    def bad(self, *argv: str, env: dict[str, str] | None = None) -> None:
        with self.assertRaises(watch.UsageError):
            watch.parse_settings(list(argv), env or {})

    def test_usage_errors(self) -> None:
        self.bad()
        self.bad("--broker", "noequals")
        self.bad("--broker", "a=ftp://x")
        self.bad("--broker", 'a=http://x"y')
        self.bad("--broker", "a=http://x", "--broker", "a=http://y")
        self.bad("--broker", "a b=http://x")
        self.bad("--broker", "a=http://x", "--threshold", "15 minutes")
        self.bad("--broker", "a=http://x", "--realert", "0m")
        self.bad("--broker", "a=http://x", "--broker-env-file", "zzz=/p")
        self.bad("--broker", "a=http://x", "--exclude", "bad node")
        self.bad("--broker", "a=http://x", "--mass-ratio", "1.5")

    def test_env_defaults(self) -> None:
        cfg = watch.parse_settings([], {
            "A2A_WORKER_WATCH_BROKERS": "t1=https://one.example/,t2=http://127.0.0.1:8787",
            "A2A_WORKER_WATCH_EDGE_ENV": "/etc/x.env",
            "A2A_WORKER_WATCH_STATE": "/s/w.json",
        })
        self.assertEqual([(b.name, b.url, b.env_file) for b in cfg.brokers],
                         [("t1", "https://one.example", "/etc/x.env"),
                          ("t2", "http://127.0.0.1:8787", "/etc/x.env")])
        self.assertEqual(cfg.state_file, "/s/w.json")
        self.assertEqual((cfg.threshold, cfg.realert), (900, (3600, 21600)))
        cfg2 = watch.parse_settings(["--broker", "a=http://x", "--broker-env-file", "a=/a.env"],
                                    {"CCC_STATE_DIR": "/st"})
        self.assertEqual(cfg2.brokers[0].env_file, "/a.env")
        self.assertEqual(cfg2.state_file, "/st/a2a-broker-worker-watch.json")

    def test_fmt_age(self) -> None:
        self.assertEqual([watch.fmt_age(s) for s in (0, 59, 61, 3600, 3725, 90000)],
                         ["0m", "0m", "1m", "1h00m", "1h02m", "1d1h"])


if __name__ == "__main__":
    unittest.main()
