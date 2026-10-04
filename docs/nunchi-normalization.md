# Nunchi recurring automation

The review flag is an unresolved question, not a count of automatic failures.
`judge.status.json` beside each facts DB distinguishes eligible work, unchanged
human holds, transient retry waits, missing decision reasons, and duplicate
proposals. The existing review report and audit retain the actionable details.
No memory is deleted or automatically merged by this scheduler.

## Review scheduling

`install-nunchi.sh --apply --judge-apply` installs an hourly `:41` run. Each run
handles at most `NUNCHI_JUDGE_CAP` (default 10). Every actual provider invocation,
including a fallback and a dry-run invocation, reserves one slot from
`NUNCHI_JUDGE_DAILY_CAP` (default 50, maximum 1000). All channels and audience
children MUST use the same node-local `CCC_STATE_DIR`: the counter is stored at
`$CCC_STATE_DIR/nunchi-judge/calls.json`, locked across processes and dated in KST.
Corrupt or unsafe counters stop provider calls. Historical daily counts remain.

Apply runs record body-free state in `nunchi_review_state` in the same audience
DB. Unchanged human/conflict results wait seven days; provider failures retry
after 15 minutes with exponential backoff capped at 24 hours. Never-reviewed
items precede due rechecks. Changes to a fact, reason, source rank, evidence,
or the set/content of its live conflict peers immediately invalidate the hold.
A mutation-time recheck under SQLite's write lock prevents applying a verdict
against evidence changed during the model call. SQLite backups include WAL data.
Missing reasons and near-duplicate proposals remain owner work outside the cap.
Dry runs spend bounded model calls but do not advance persistent dispositions.

## Danso cron authentication

The installer selects the native judge for `--danso`; other providers retain
Claude/Codex selection, and Jev remains explicitly opt-in. It uses the bridge
venv and the same tool-free, one-turn isolated Danso subprocess as extraction.
Model output is untrusted verdict data; no tools or persistent sessions are used.

Configure `~/.config/ccc-node/nunchi-judge.json` (owned by the runtime user, 0600)
with **references to existing protected service environment files**, in their
normal precedence order. Do not copy credentials into this manifest:

```json
{"environment_files":["/home/agent/.config/provider/runtime.env","/home/agent/.config/provider/pinned-cli.env"]}
```

The helper reads only Danso settings, the provider key, and project-root fields;
process settings take precedence. Both the manifest and referenced files reject
symlinks and group/other access. Runtime user/home must match the service owner.
Without a manifest, `CCC_NUNCHI_JUDGE_ENV_FILE` can select one file; its default
is `~/.config/ccc-node/bridge.env`. Missing configuration fails closed. Verify
an actual verdict from the exact cron identity/environment before rollout.

## Extraction accounting and recovery

Admission still reserves the conservative complete-request bound before work
starts. Matching `DANSO_USAGE` and `PIRI_USAGE` CLI diagnostics supply trusted
actual usage; input includes cache read/write tokens. Model-authored usage is
ignored. The reservation is replaced exactly once, on its original KST day,
under the shared meter lock. Actual overages are recorded in full. Missing,
invalid, failed or cancelled provider results retain the conservative charge.
The journal separately reports estimated bounds and actual observed usage.

`CCC_USAGE_RECOVERY_RESERVE_PERCENT` (0 by default, 0–50 allowed) partitions an
existing finite per-provider autonomous cap. A 25% reserve of a 1,000,000-token
cap permits 750,000 routine and 250,000 recovery tokens, with a shared 1,000,000
aggregate ceiling. Recovery workers explicitly use `budget_purpose="recovery"`;
normal scheduled workers remain routine. A recovery request without a positive
finite reserved allowance is rejected. Both bridge channels must use the same
meter and policy. A crash or unknown usage stays charged; never reset historical
counters to manufacture capacity. Each partition must fit one maximal request.

## Acceptance after deployment

Stage one runtime/channel, then all nodes. Capture per audience: source input
timestamp, active versus terminal extraction jobs, local sink completion and
source provenance, snapshot freshness, review status, provider failures and
actual/unknown usage. Terminal recovered jobs are historical receipts, not new
active work; missing source material is an explicit unrecoverable exception.

Observe natural scheduled execution for 24–48 hours without manual queue
draining. Require new input to reach the correct DB and snapshot, no unmatched
completed extraction, no repeated auth failure, no unchanged-row starvation,
and spending within configured admission limits. Human holds need disposition,
not forced flag clearing. A channel with no real input remains unverified until
its first natural input. Jev quality requires actual production decisions and
must not be inferred from filtered-event volume.
