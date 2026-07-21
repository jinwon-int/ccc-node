# Bridge quality gates and measured baseline (issue #348)

Source-side record of the regression gates added by #348 and the baseline they
were calibrated against. Update this file whenever a gate value changes — the
gates themselves live in config (`pyproject.toml`, `bridge/pyproject.toml`),
this file records the *why* and the measured context.

## Gates

| Gate | Value | Enforced by |
| --- | --- | --- |
| Branch coverage floor | `fail_under = 72` | `bridge/pyproject.toml` `[tool.coverage.report]`, applied to the CI `--cov` run in `bridge-tests` |
| Cyclomatic complexity | `C901`, `max-complexity = 15` | root `pyproject.toml` ruff config, `python-lint` job |
| Type checking | all of `bridge/core` + contract tests + agent-cron schedule/schema/model/repository modules | root `pyproject.toml` `[tool.mypy]`, `python-lint` job |

## Coverage

Measured on the #348 branch with Python 3.11 (branch coverage enabled — the
gated metric; the earlier 69% in the issue and 77% pre-#348 were *line*
coverage and are not comparable):

- Before the #348 path tests: **74.1%** branch coverage (861 tests).
- Low modules at that point: `bot_status` 25.5%, `bot_commands` 47.7%,
  `bot_delivery` 47.9%, `bot_access` 76.8%.
- After the #348 behavior tests (`test_bot_access_paths`,
  `test_bot_status_callback`, `test_bot_delivery_paths` — targeting exactly
  the deny/error/empty/no-match branches of the access and delivery paths):
  **75.1%** total (899 tests); `bot_access` 93.4%, `bot_status` 97.9%,
  `bot_delivery` 57.1%. `bot_commands` (47.7%) is the next staged target.

**Floor policy**: `fail_under` stays ~2 points under the measured total so
environment noise cannot flake the gate, and is raised in the same PR whenever
a change lifts the measured total by more than that buffer. Staged targets:
72 (now) → 75 → 80 as `bot_commands`/`bot_delivery` gain behavior tests.
Never lower the floor to admit a regression; lowering requires an issue that
explains why the coverage was lost.

## Complexity baseline (C901 > 15)

New or edited functions above CC 15 fail `python-lint`. The pre-existing
hotspots below are explicitly marked `# noqa: C901 -- #348 baseline hotspot`
at the definition site, so removing or adding a marker is always visible in
review. Refactors should remove markers over time; never add one to new code.

19 baseline markers at introduction: `bot.py::_process_user_message_text` (17),
`bot_delivery.py::_handle_callback` (30), `bot_lifecycle.py::_run_async` (22),
`bot_voice.py::_handle_voice_message` (24), `bot_voice.py::run_task` (20),
`codex_app_server.py::_read_stdout` (16),
`dead_session_recovery.py::recover_dead_session_notifications` (24),
`project_chat.py::_create_user_stream` (16),
`project_chat.py::_disconnect_stream_state` (17),
`project_chat_process.py::_process_agent_message` (41),
`project_chat_process.py::process_message` (20),
`project_chat_reader.py::_reader_loop` (37),
`streaming.py::finalize_draft` (16), `utils/tg_readable.py::_transform` (23),
`scripts/a2a_termux_native_worker.py::validate_env` (18),
`scripts/agent_cron.py::due_plan` (18),
`scripts/ccc_doctor.py::diagnose` (22), `scripts/ccc_doctor.py::main` (16).

Retired since introduction (function deleted with the legacy direct Claude
SDK path, #584 slice C-2): `project_chat.py::_create_user_stream`,
`project_chat.py::_disconnect_stream_state`,
`project_chat_process.py::process_message` (rewritten below the threshold),
`project_chat_reader.py::_reader_loop` (module deleted).

## Mypy scope

All `bridge/core` modules and the typed agent-cron domain modules are checked. The `TelegramBot` mixin modules
(`bot_access`, `bot_commands`, `bot_delivery`, `bot_lifecycle`, `bot_status`,
`bot_voice`, `project_chat_history`) reference attributes provided by the
composing class, which mypy cannot see until the typed-composition/AppContext
refactor lands; **only** `attr-defined` is disabled for **only** those modules,
via `[[tool.mypy.overrides]]` in the root `pyproject.toml` — any change to
that list is review-visible in config. Every other error class gates those
modules, and all other core modules get full checking.

## Test time and flakiness baseline

Measured locally (Python 3.11, single process) at #348 introduction:

- Bridge suite with branch coverage: 75.6 s and 76.6 s wall clock across two
  back-to-back runs (899 passed, 2 skipped, 112 subtests each).
- Full repo suite (bridge + root hardening tests, no coverage): ~70 s.
- Flakiness: the two coverage runs produced byte-identical totals and
  identical pass/fail results (zero flaky tests observed; the only failure
  seen during #348 development was build-artifact pollution from a local
  wheel build, not test nondeterminism). CI `bridge-tests` wall-clock
  reference at the time: ~1.5–2 min per matrix leg.

Re-measure and update this section when the suite grows by more than ~20% or
CI runtimes drift noticeably.
