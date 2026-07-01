# Security audit operations

`ccc-security-audit.sh` is a read-only metadata-oriented checker for ccc-node harness safety. It should classify risk without printing matched secret values, memory contents, raw environment, or raw Telegram/provider payloads.

## Local use

Run the local checker from the repo or installed node surface:

```bash
bash scripts/ccc-security-audit.sh
```

Use JSON output where available for automation and fleet rollups.

## Fleet rollup

Collect per-node output into block evidence and summarize with:

```bash
bash scripts/ccc-security-audit-fleet-matrix.sh --evidence fleet-security.txt --json
```

The fleet matrix is read-only. It does not SSH, change permissions, restart services, send providers, read secrets, or mutate node state. It classifies already-collected evidence into `정상`, `경고`, `교정가능`, `수동필요`, or `위험`.

## Reporting boundary

Report counts, statuses, file modes, safe reason tags, and remediation class. Do not include raw matched lines if they may contain credentials or private memory.
