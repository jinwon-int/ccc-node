# Report-style canary — STE 80% rule (#2109 A)

Origin: Andrej Karpathy, X 2026-10-02 — the time spent *understanding* LLM
output grows; the first lever is controlled-language writing (ASD-STE100,
"80%" of it is enough). Issue #2109 item A trials that rule on owner-facing
reports before changing the fleet-wide `ccc-report` output style.

## How it works

- `claude/hooks/report-style-canary.sh` runs at `SessionStart` and
  `PostCompact` (wired in `claude/settings.base.json`). It injects
  `claude/hooks/lib/report-style-ste.txt` as `additionalContext` **only when**
  `~/.claude/state/report-style-canary.flag` exists on that node. Without the
  flag the hook prints nothing and exits 0, so shipping it fleet-wide changes
  no node's behaviour.
- The rule text keeps the `ccc-report` section structure (확정 사실 / 변경 /
  리스크 / 다음 액션) and the evidence/ID rules; it only constrains the
  owner-facing prose (≤20 words per sentence, one idea per sentence, active
  voice, one term per concept, numbers/IDs verbatim, bullets over parentheses).
- `*.md` under `claude/hooks/` is not deployed (`scripts/lib/harness-paths.sh`),
  which is why the rule body is a `.txt` under `hooks/lib/`.
- Codex's memory materializer reads the same installed flag/rule for direct
  `ccc-codex` runs and bridge session starts/resumes. It puts the prose policy
  **before** the untrusted memory section. Flag/rule changes invalidate the
  otherwise unchanged snapshot, and stale policy cannot pass fallback readiness.
- Danso's Telegram/Matrix bridge composes a private `--system-context-file`
  on each dispatch, including resumed turns, in memory-off, materializer and
  native-read modes. It uses the node's `claude_settings_path`, not the isolated
  provider HOME. Audience files remain separate; the style precedes memory.
  If style plus memory exceeds the native 32 KiB file budget, memory is retained
  and the optional style is omitted. Raw standalone Danso runs are not wired.
- Python consumers use `bridge/utils/report_style.py` (also installed as
  `hooks/ccc_report_style.py`). The text still has one source. Missing, invalid,
  symlinked or group/world-writable flag/rule files are ignored. The flag is
  bounded to 4 KiB, the rule to 16 KiB, and the note to 160 characters.
  `CLAUDE_DISTILL_INFLIGHT` suppresses injection in all three providers.

## Operating the canary

```bash
# arm on a canary node (the first non-empty line is echoed to the model as a note)
printf 'end: 2026-10-09 18:00 KST — judge: owner (#2109)\n' > ~/.claude/state/report-style-canary.flag
# disarm
rm ~/.claude/state/report-style-canary.flag
```

A Claude session start or compaction picks the change up. Codex refreshes on
the next managed invocation/session start or resume; Danso on the next dispatch.
Already supplied conversation context is not erased by removing the flag:
start a fresh session after the trial when a clean control is required.
Arming is node-local, reversible and outside the harness
drift surface (`ccc-doctor` compares settings/hook wiring, not state files).

Time-boxed test rule (from #2109): when the canary starts, pin on the issue —
start time, **absolute end time in KST**, the metrics (owner's felt
comprehension time + re-question count, average report/sentence length) and
the judge (owner). Canary on 1–2 nodes first, then the fleet.
The timestamp in the flag is an operator note, not an automatic expiry. Book
a persistent stop/observation job when activating the trial. Record whether
the comparison is a before/after baseline or randomized A/B; do not report
heuristic sentence measurements as ASD-STE100 certification or infer the
owner's comprehension verdict from report length alone.

## After the verdict

- **Adopt**: move the rule block into `claude/output-styles/ccc-report.md`,
  delete the hook, the wiring and the flag files, and record the decision in
  the Family Wiki (`pages/decisions/` + LOG).
- **Reject / rework**: delete the flags; the hook stays inert, or remove it in
  the same PR that records the decision.

## Tests

`claude/hooks/report-style-canary.test.sh` — silent without the flag, injects
with it, bounded operator note, `PostCompact` re-injection, other events
ignored, missing rule text stays silent, distill-subprocess guard, always
exit 0.

Provider coverage: `scripts/ccc_codex_memory_test.py`,
`bridge/tests/test_report_style.py`, and `bridge/tests/test_danso_memory.py`
exercise activation, disarm, unchanged memory, rule refresh, distill exclusion,
the three Danso memory modes, private/shared isolation, native file limits,
and equality with the installed Claude hook's output.
