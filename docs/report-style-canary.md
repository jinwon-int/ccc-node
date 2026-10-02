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

## Operating the canary

```bash
# arm on a canary node (the first non-empty line is echoed to the model as a note)
printf 'end: 2026-10-09 18:00 KST — judge: owner (#2109)\n' > ~/.claude/state/report-style-canary.flag
# disarm
rm ~/.claude/state/report-style-canary.flag
```

A new session (or the next compaction) picks the change up; a running
session does not. Arming is node-local, reversible and outside the harness
drift surface (`ccc-doctor` compares settings/hook wiring, not state files).

Time-boxed test rule (from #2109): when the canary starts, pin on the issue —
start time, **absolute end time in KST**, the metrics (owner's felt
comprehension time + re-question count, average report/sentence length) and
the judge (owner). Canary on 1–2 nodes first, then the fleet.

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
