#!/usr/bin/env python3
"""Hermetic regressions for managed TM-2380 auto-distill (#1257)."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from model_command import (  # noqa: E402
    CLAUDE_ARGS,
    PIRI_ARGS,
    ModelCommandError,
    codex_scratch_home,
    parse_systemd_environment,
    read_bridge_unit_environment,
    resolve_explicit_model_command,
    resolve_model_command,
)


def _load_auto_distill():
    spec = importlib.util.spec_from_file_location(
        "managed_auto_distill", HERE / "auto-distill.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load managed auto-distill")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AUTO_DISTILL = _load_auto_distill()


class ModelCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def executable(self, name: str) -> Path:
        path = self.root / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o700)
        return path

    @staticmethod
    def no_which(_name: str) -> None:
        return None

    def test_systemd_parser_returns_only_allowlisted_values(self) -> None:
        parsed = parse_systemd_environment(
            'CCC_AGENT_PROVIDER=piri '
            '"CCC_PIRI_REAL_CLI_PATH=/opt/piri path/piri-ccc.sh" '
            'UNRELATED_SECRET=do-not-return'
        )
        self.assertEqual(parsed["CCC_AGENT_PROVIDER"], "piri")
        self.assertEqual(
            parsed["CCC_PIRI_REAL_CLI_PATH"], "/opt/piri path/piri-ccc.sh"
        )
        self.assertNotIn("UNRELATED_SECRET", parsed)

    def test_systemd_reader_prefers_complete_system_unit(self) -> None:
        calls: list[list[str]] = []

        def runner(argv, **_kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=(
                    "CCC_AGENT_PROVIDER=piri "
                    "CCC_PIRI_REAL_CLI_PATH=/opt/piri/piri-ccc.sh"
                ),
                stderr="",
            )

        values = read_bridge_unit_environment(runner=runner)
        self.assertEqual(values["CCC_AGENT_PROVIDER"], "piri")
        self.assertEqual(values["CCC_PIRI_REAL_CLI_PATH"], "/opt/piri/piri-ccc.sh")
        self.assertEqual(len(calls), 1)

    def test_systemd_reader_does_not_mix_two_units(self) -> None:
        calls = 0

        def runner(argv, **_kwargs):
            nonlocal calls
            calls += 1
            stdout = (
                "CCC_AGENT_PROVIDER=piri"
                if calls == 1
                else "CCC_PIRI_CLI_PATH=/stale/user-wrapper"
            )
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

        values = read_bridge_unit_environment(runner=runner)
        self.assertEqual(values, {"CCC_AGENT_PROVIDER": "piri"})
        self.assertEqual(calls, 1)

    def test_process_real_path_wins(self) -> None:
        process_real = self.executable("process-real")
        unit_real = self.executable("unit-real")
        wrapper = self.executable("wrapper")
        selected = resolve_model_command(
            process_environment={
                "CCC_AGENT_PROVIDER": "piri",
                "CCC_PIRI_REAL_CLI_PATH": str(process_real),
                "CCC_PIRI_CLI_PATH": str(wrapper),
            },
            unit_environment={"CCC_PIRI_REAL_CLI_PATH": str(unit_real)},
            home=self.home,
            which=self.no_which,
            piri_default_paths=(),
        )
        self.assertEqual(selected.engine, "piri")
        self.assertEqual(selected.argv, (str(process_real), *PIRI_ARGS))
        self.assertEqual(selected.source, "process:CCC_PIRI_REAL_CLI_PATH")

    def test_systemd_real_path_beats_process_wrapper(self) -> None:
        unit_real = self.executable("unit-real")
        process_wrapper = self.executable("process-wrapper")
        selected = resolve_model_command(
            process_environment={
                "CCC_AGENT_PROVIDER": "piri",
                "CCC_PIRI_CLI_PATH": str(process_wrapper),
            },
            unit_environment={"CCC_PIRI_REAL_CLI_PATH": str(unit_real)},
            home=self.home,
            which=self.no_which,
            piri_default_paths=(),
        )
        self.assertEqual(selected.argv[0], str(unit_real))
        self.assertEqual(selected.source, "systemd:CCC_PIRI_REAL_CLI_PATH")

    def test_standard_opt_like_path_beats_home_path(self) -> None:
        opt_path = self.executable("opt-piri")
        home_path = self.executable("home-piri")
        selected = resolve_model_command(
            process_environment={"CCC_AGENT_PROVIDER": "piri"},
            unit_environment={},
            home=self.home,
            which=self.no_which,
            piri_default_paths=(opt_path, home_path),
        )
        self.assertEqual(selected.argv[0], str(opt_path))
        self.assertEqual(selected.source, f"standard:{opt_path}")

    def test_piri_provider_refuses_claude_fallback(self) -> None:
        claude = self.executable("claude")

        def which(name: str) -> str | None:
            return str(claude) if name == "claude" else None

        with self.assertRaisesRegex(ModelCommandError, "refusing Claude fallback"):
            resolve_model_command(
                process_environment={"CCC_AGENT_PROVIDER": "piri"},
                unit_environment={},
                home=self.home,
                which=which,
                piri_default_paths=(),
            )

    def test_claude_provider_does_not_switch_to_available_piri(self) -> None:
        piri = self.executable("piri")
        claude = self.executable("claude")

        def which(name: str) -> str | None:
            return {"piri": str(piri), "claude": str(claude)}.get(name)

        selected = resolve_model_command(
            process_environment={"CCC_AGENT_PROVIDER": "claude"},
            unit_environment={},
            home=self.home,
            which=which,
            piri_default_paths=(),
        )
        self.assertEqual(selected.engine, "claude")
        self.assertEqual(selected.argv, (str(claude), *CLAUDE_ARGS))

    def test_codex_runtime_hint_keeps_auto_piri_lane(self) -> None:
        piri = self.executable("piri")
        selected = resolve_model_command(
            process_environment={"CCC_AGENT_PROVIDER": "codex"},
            unit_environment={"CCC_PIRI_REAL_CLI_PATH": str(piri)},
            home=self.home,
            which=self.no_which,
            piri_default_paths=(),
        )
        self.assertEqual(selected.engine, "piri")

    def test_auto_claude_fallback_is_explicit(self) -> None:
        claude = self.executable("claude")

        def which(name: str) -> str | None:
            return str(claude) if name == "claude" else None

        selected = resolve_model_command(
            process_environment={},
            unit_environment={},
            home=self.home,
            which=which,
            piri_default_paths=(),
        )
        self.assertEqual(selected.engine, "claude")
        self.assertEqual(selected.reason, "no-runnable-piri")

    def test_invalid_dedicated_provider_fails_closed(self) -> None:
        # "codex" became a valid dedicated provider in #1295; use a still-
        # invalid value to pin the validation error.
        with self.assertRaisesRegex(ModelCommandError, "must be auto"):
            resolve_model_command(
                process_environment={"CCC_AUTO_DISTILL_PROVIDER": "gpt"},
                unit_environment={},
                home=self.home,
                which=self.no_which,
                piri_default_paths=(),
            )

    def test_explicit_command_preserves_quoted_argument(self) -> None:
        custom = self.executable("custom")
        selected = resolve_explicit_model_command(
            f'{custom} --label "two words"', which=self.no_which
        )
        self.assertEqual(selected.argv, (str(custom), "--label", "two words"))

    def test_extractor_child_always_receives_inflight_guards(self) -> None:
        completed = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"items": []}),
            stderr="",
        )
        with patch.object(AUTO_DISTILL.subprocess, "run", return_value=completed) as run:
            data, (error, _usage, _raw) = AUTO_DISTILL.extract_json(
                "prompt", ["model"], 10
            )
        self.assertEqual(data, {"items": []})
        self.assertIsNone(error)
        child_env = run.call_args.kwargs["env"]
        self.assertEqual(child_env["CLAUDE_DISTILL_INFLIGHT"], "1")
        self.assertEqual(child_env["CCC_AUTO_DISTILL_INFLIGHT"], "1")

    def test_spawn_failure_is_body_and_path_free(self) -> None:
        with patch.object(AUTO_DISTILL.subprocess, "run", side_effect=FileNotFoundError):
            _data, (error, _usage, raw) = AUTO_DISTILL.extract_json(
                "secret prompt", ["/private/path/model"], 10
            )
        self.assertEqual(error, "model_spawn_error:FileNotFoundError")
        self.assertEqual(raw, "")
        self.assertNotIn("/private/path", error)

    def test_identifier_regex_is_linear_and_keeps_marker_contract(self) -> None:
        identifier = AUTO_DISTILL.TOKEN_RES[3]
        accepted = [
            "providerGuard",
            "provider_guard",
            "provider2",
            "A2A",
            "guardFile.sh",
        ]
        rejected = ["observation", "primitives", "abcdef"]
        self.assertEqual(
            [identifier.fullmatch(value) is not None for value in accepted],
            [True] * len(accepted),
        )
        self.assertEqual(
            [identifier.fullmatch(value) is not None for value in rejected],
            [False] * len(rejected),
        )
        long_digit_run = "a" + ("0" * 100_000)
        self.assertIsNotNone(identifier.fullmatch(long_digit_run))

    def test_main_reports_custom_engine_and_writes_body_free_audit(self) -> None:
        custom = self.executable("custom")
        session_dir = self.home / ".piri/agent/sessions"
        session_dir.mkdir(parents=True)
        environment = os.environ.copy()
        environment.update({"HOME": str(self.home), "PATH": "/usr/bin:/bin"})
        completed = subprocess.run(
            [
                sys.executable,
                str(HERE / "auto-distill.py"),
                "--dry-run",
                "--cap",
                "0",
                "--no-cache-sync",
                "--model-cmd",
                str(custom),
            ],
            capture_output=True,
            text=True,
            timeout=20,
            env=environment,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("engine=custom source=--model-cmd", completed.stdout)
        audit_path = self.home / ".hermes/logs/auto-distill-audit.jsonl"
        audit = [json.loads(line) for line in audit_path.read_text().splitlines()]
        self.assertEqual(audit[0]["event"], "engine_selected")
        self.assertEqual(audit[0]["engine"], "custom")
        self.assertNotIn("argv", audit[0])


class IterMessagesSinceLineTest(unittest.TestCase):
    """iter_messages(since_line=...) skips already-processed lines pre-parse."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def message(index: int, role: str = "user") -> str:
        return json.dumps({
            "type": "message",
            "id": "m%07d" % index,
            "message": {"role": role, "content": "line %d" % index},
        })

    def write_session(self, lines: list[str]) -> Path:
        path = self.root / "session.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_default_scan_yields_every_message_with_unchanged_shape(self) -> None:
        path = self.write_session([self.message(1), self.message(2, "assistant")])
        got = list(AUTO_DISTILL.iter_messages(str(path)))
        self.assertEqual(
            [(1, "m0000001", "user", "line 1"), (2, "m0000002", "assistant", "line 2")],
            got,
        )
        self.assertTrue(AUTO_DISTILL.iter_messages.last_schema_ok)

    def test_since_line_skips_lines_before_json_parse(self) -> None:
        path = self.write_session([
            "{this line is not JSON and must never reach json.loads",
            self.message(2),
            self.message(3, "assistant"),
            self.message(4),
        ])
        parsed: list[str] = []
        real_loads = json.loads

        def counting_loads(text, *args, **kwargs):
            parsed.append(text)
            return real_loads(text, *args, **kwargs)

        with patch.object(AUTO_DISTILL.json, "loads", side_effect=counting_loads):
            got = list(AUTO_DISTILL.iter_messages(str(path), since_line=2))
        self.assertEqual(
            [(3, "m0000003", "assistant", "line 3"), (4, "m0000004", "user", "line 4")],
            got,
        )
        # Lines 1-2 (the malformed line included) were skipped before parsing.
        self.assertEqual(len(parsed), 2)
        self.assertTrue(all("line is not JSON" not in text for text in parsed))

    def test_since_line_past_content_marks_schema_ok(self) -> None:
        path = self.write_session([self.message(1), self.message(2)])
        got = list(AUTO_DISTILL.iter_messages(str(path), since_line=2))
        self.assertEqual(got, [])
        # A positive watermark only exists because a previous run recognized
        # the schema, so an empty increment must not read as unknown_schema.
        self.assertTrue(AUTO_DISTILL.iter_messages.last_schema_ok)

    def test_digest_session_reads_only_the_increment(self) -> None:
        path = self.write_session(
            [self.message(1), self.message(2, "assistant"), self.message(3)]
        )
        digest, ids, total = AUTO_DISTILL.digest_session(str(path), since_line=1)
        self.assertEqual(total, 3)
        self.assertNotIn("line 1", digest)
        self.assertIn("line 2", digest)
        self.assertIn("line 3", digest)
        self.assertEqual(sorted(ids), ["m0000002", "m0000003"])

    def test_digest_session_empty_increment_keeps_watermark_total(self) -> None:
        path = self.write_session([self.message(1), self.message(2)])
        digest, ids, total = AUTO_DISTILL.digest_session(str(path), since_line=2)
        self.assertEqual(digest, "")
        self.assertEqual(ids, {})
        # total must stay at the watermark, not collapse to 0 (a 0 watermark
        # would make every later run reprocess the whole file).
        self.assertEqual(total, 2)


class CodexRolloutParserTest(unittest.TestCase):
    """iter_messages recognizes the Codex CLI rollout schema (#1295)."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_rollout(self, lines: list[str]) -> Path:
        path = self.root / "rollout.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    @staticmethod
    def response_item(idx: int, role: str, text: str) -> str:
        return json.dumps({
            "timestamp": "2026-08-26T00:00:00Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "msg_%03d" % idx,
                "role": role,
                "content": [{"type": "output_text" if role == "assistant" else "input_text", "text": text}],
            },
        })

    def test_codex_messages_yield_with_id_and_role(self) -> None:
        path = self.write_rollout([
            json.dumps({"type": "session_meta", "payload": {"session_id": "s1"}}),
            self.response_item(1, "user", "위키 레포 미머지 피알 머지하자"),
            self.response_item(2, "assistant", "머지 완료했습니다"),
        ])
        got = list(AUTO_DISTILL.iter_messages(str(path)))
        self.assertEqual(
            [(2, "msg_001", "user", "위키 레포 미머지 피알 머지하자"),
             (3, "msg_002", "assistant", "머지 완료했습니다")],
            got,
        )
        self.assertTrue(AUTO_DISTILL.iter_messages.last_schema_ok)

    def test_non_message_response_items_are_skipped_but_schema_stays_ok(self) -> None:
        path = self.write_rollout([
            self.response_item(1, "user", "질문"),
            json.dumps({"type": "response_item", "payload": {"type": "reasoning"}}),
            json.dumps({"type": "response_item", "payload": {"type": "custom_tool_call", "id": "t1"}}),
            json.dumps({"type": "event_msg", "payload": {"type": "token_count"}}),
        ])
        got = list(AUTO_DISTILL.iter_messages(str(path)))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][2], "user")
        self.assertTrue(AUTO_DISTILL.iter_messages.last_schema_ok)

    def test_unknown_file_is_not_schema_ok(self) -> None:
        path = self.write_rollout(["not json at all", "{\"type\": \"world_state\"}"])
        got = list(AUTO_DISTILL.iter_messages(str(path)))
        self.assertEqual(got, [])
        self.assertFalse(AUTO_DISTILL.iter_messages.last_schema_ok)


class CodexScratchHomeTest(unittest.TestCase):
    """codex_scratch_home symlinks auth/config and fails closed without auth."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_missing_auth_fails_closed(self) -> None:
        with self.assertRaisesRegex(ModelCommandError, "auth.json"):
            codex_scratch_home(home=self.home)

    def test_scratch_symlinks_auth_and_config(self) -> None:
        real = self.home / ".codex"
        real.mkdir()
        (real / "auth.json").write_text("{}", encoding="utf-8")
        (real / "config.toml").write_text("model=x", encoding="utf-8")
        scratch = codex_scratch_home(home=self.home)
        self.assertEqual(scratch, self.home / ".codex-auto-distill-scratch")
        link = scratch / "auth.json"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.path.realpath(link), str(real / "auth.json"))
        self.assertTrue((scratch / "config.toml").is_symlink())


class CodexResolverTest(unittest.TestCase):
    """provider=codex resolves the codex engine and never falls back."""

    def test_explicit_codex_provider_requires_codex_cli(self) -> None:
        with self.assertRaisesRegex(ModelCommandError, "refusing Piri/Claude fallback"):
            resolve_model_command(
                process_environment={"CCC_AUTO_DISTILL_PROVIDER": "codex"},
                which=lambda _: None,
            )

    def test_dedicated_provider_validation_accepts_codex(self) -> None:
        with self.assertRaisesRegex(ModelCommandError, "must be auto"):
            resolve_model_command(
                process_environment={"CCC_AUTO_DISTILL_PROVIDER": "gpt"},
                which=lambda _: None,
            )


class LiteralHitsIndexTest(unittest.TestCase):
    """One-pass grep index preserves per-item literal_hits semantics."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.pages = Path(self.temp.name) / "wiki-cache" / "pages"
        (self.pages / "log").mkdir(parents=True)
        (self.pages / "nodes" / "x").mkdir(parents=True)
        (self.pages / "log" / "alpha.md").write_text(
            "# doc\n"
            "## TM-2380-foo rollout\n"
            "GUARD_TOKEN appears with detail\n",
            encoding="utf-8",
        )
        (self.pages / "log" / "beta.md").write_text(
            "intro\n"
            "## beta section\n"
            "GUARD_TOKEN appears again\n",
            encoding="utf-8",
        )
        # Staging page: must be indexable but filtered out of literal_hits.
        (self.pages / "nodes" / "x" / "AUTO.md").write_text(
            "TM-2380-foo staged copy with GUARD_TOKEN\n", encoding="utf-8"
        )
        self.cache_patch = patch.object(
            AUTO_DISTILL, "WIKI_CACHE", str(self.pages)
        )
        self.cache_patch.start()

    def tearDown(self) -> None:
        self.cache_patch.stop()
        self.temp.cleanup()

    def test_index_records_first_match_line_per_file(self) -> None:
        index = AUTO_DISTILL.build_literal_index(["GUARD_TOKEN"])
        hits = {path: lineno for path, lineno, _text in index["GUARD_TOKEN"]}
        self.assertEqual(
            hits,
            {
                str(self.pages / "log" / "alpha.md"): 3,
                str(self.pages / "log" / "beta.md"): 3,
                str(self.pages / "nodes" / "x" / "AUTO.md"): 1,
            },
        )

    def test_overlapping_tokens_are_both_indexed(self) -> None:
        # A grep -o pass would report only the longest match at a position;
        # substring assignment must keep BOTH tokens for the same line.
        index = AUTO_DISTILL.build_literal_index(["TM-2380", "TM-2380-foo"])
        alpha = str(self.pages / "log" / "alpha.md")
        self.assertIn((alpha, 2), [(p, n) for p, n, _t in index["TM-2380"]])
        self.assertIn((alpha, 2), [(p, n) for p, n, _t in index["TM-2380-foo"]])

    def test_literal_hits_with_shared_index_matches_self_built(self) -> None:
        fact = "TM-2380-foo rollout uses GUARD_TOKEN"
        tokens = AUTO_DISTILL._collect_literal_tokens([{"fact": fact}])
        shared = AUTO_DISTILL.build_literal_index(tokens)
        self.assertEqual(
            AUTO_DISTILL.literal_hits(fact),
            AUTO_DISTILL.literal_hits(fact, index=shared),
        )

    def test_literal_hits_excludes_staging_and_formats_relative_paths(self) -> None:
        hits = AUTO_DISTILL.literal_hits("GUARD_TOKEN observed")
        self.assertTrue(hits)
        self.assertTrue(all("AUTO.md" not in hit for hit in hits))
        self.assertTrue(
            all(hit.startswith("pages/log/") and " :: [리터럴 `GUARD_TOKEN`] " in hit
                for hit in hits)
        )

    def test_literal_hits_honours_limit(self) -> None:
        hits = AUTO_DISTILL.literal_hits("GUARD_TOKEN observed", limit=1)
        self.assertEqual(len(hits), 1)

    def test_missing_cache_dir_fails_open_to_empty(self) -> None:
        with patch.object(AUTO_DISTILL, "WIKI_CACHE", str(self.pages / "absent")):
            self.assertEqual(AUTO_DISTILL.literal_hits("GUARD_TOKEN observed"), [])
            self.assertEqual(
                AUTO_DISTILL.build_literal_index(["GUARD_TOKEN"]),
                {"GUARD_TOKEN": []},
            )

    def test_section_body_lines_are_cached_per_run(self) -> None:
        target = self.pages / "log" / "cached.md"
        target.write_text("## cached head\nGUARD_TOKEN body line\n", encoding="utf-8")
        first = AUTO_DISTILL.section_body(str(target), lineno=2)
        self.assertIn("GUARD_TOKEN", first)
        target.unlink()
        # The wiki cache is static within a run; the cached lines must serve
        # repeat lookups without re-reading the file.
        self.assertEqual(AUTO_DISTILL.section_body(str(target), lineno=2), first)


if __name__ == "__main__":
    unittest.main()
