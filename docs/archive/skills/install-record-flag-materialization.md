# [ARCHIVED SKILL] install-record-flag-materialization

> **Archived:** 2026-08-28 — retroactive skill audit round 1 (ccc-node#1347).  
> **Reason:** 일회성 설치자 절차(해결됨). 2026-08-28 감사(#1347)에서 본 노드 직접 로드 0건 확인.  
> **Original location:** `claude/skills/install-record-flag-materialization/SKILL.md`  
> **Original description:** Make an opt-in installer flag survive self-update by materializing it into the ccc.install-record.v1 argv, then re-run the installer on each node so its record carries the flag. Use when adding an opt-in flag to a ccc-node installer, when an approved per-node setting silently reverted, or when a managed cron line is missing on nodes that should have it.

Skill routing에서 퇴역해 세션 컨텍스트에 더는 노출되지 않는다. 내용은 참조용으로 보존.

---

# When to Use

- You are adding an **opt-in** flag to a `scripts/install-*.sh` installer (one that
  adds a cron line, a hook, or a service only when passed).
- An approved per-node setting turned itself off with no error in the log.
- A managed cron line is present on some fleet nodes and missing on others.
- You merged an installer fix and need the fleet's install records to reflect it.

# Core insight

**The install record is the source of truth for replay — not the crontab, and not the
node's current state.**

`scripts/ccc-self-update.sh` watches each record's `gen` stamp. When the installer's
content hash drifts, it **replays** the installer using the record's stored `argv`
(`ccc-self-update.sh:35`, `:560`). The installer's `strip_cron` first removes *all*
managed lines, and only the recorded flags re-add them.

So an opt-in flag that is not in `record_argv` is not merely "not remembered" — the
next replay **deletes** the thing it enabled. Silently. Exit code 0.

This is why hand-editing the managed cron line never works: the edit lives outside the
record, and the next replay rewrites over it. It is also why an approved pilot can
switch itself back off with nothing in any log.

Measured 2026-08-25 (#1264): the judge-batch cron was live on **1 of 11** fleet nodes,
while every node carried the script and a non-empty review queue. The omission of
`--judge` from `record_argv` is the mechanism that lost it.

# Procedure

## Part A — materialize the flag in the installer

1. **Build `record_argv` from resolved values, never from ambient env.** Re-deriving at
   replay time silently un-scopes anything the operator scoped explicitly.

   ```bash
   record_argv=(--apply "--$resolved_provider")
   if [ "$AUDIENCE_SCOPED" = 1 ]; then record_argv+=(--audience-scoped "$AUDIENCE_ROOT"); fi
   if [ "$JUDGE_APPLY" = 1 ]; then record_argv+=(--judge-apply)
   elif [ "$JUDGE" = 1 ]; then record_argv+=(--judge); fi
   ccc_installer_record_write "$STATE" "$INSTALLER_PATH" "$MARK" "$GEN" -- "${record_argv[@]}"
   ```

2. **Handle flag implication explicitly.** If `--judge-apply` implies `--judge`, record
   the *stronger* one alone — recording the weaker one downgrades apply mode to dry-run
   on replay, which is a silent capability loss, not a crash.

3. **Comment the mechanism at the `record_argv` site.** The next person to add a flag
   reads that block; if the rule isn't written there, they reintroduce the bug.

4. **Add a replay test** — write a record with a drifted `gen`, run self-update, assert
   the flag's artifact still exists afterward. See `ccc-self-update.test.sh:545`.

## Part B — sync the fleet's records

5. **Read each node's current record before touching it** — this is the authoritative
   list of flags to preserve:

   ```bash
   python3 -c "import json,os;p=os.path.expanduser('~/.claude/state/install-nunchi.json');print(json.load(open(p))['argv'])"
   # -> ['--apply', '--claude', '--judge-apply']
   ```

   Records live at `~/.claude/state/install-<name>.json`, schema
   `ccc.install-record.v1`, with fields `installer`, `marker`, `gen`, `argv`,
   `applied_at`. There is **no** `~/.claude/install.log`.

6. **Back up the record and the crontab** before re-running:
   `cp ~/.claude/state/install-nunchi.json ~/.claude/backups/install-nunchi-$(date +%Y%m%d)-pre.json`
   and `crontab -l > ~/.claude/state/crontab.bak-<reason>-$(date -u +%Y%m%dT%H%M%SZ)`.

7. **Re-run with the preserved flags plus the new one.** These installers are
   idempotent, but note there is **no `--dry-run` option** — `--dry-run` in this
   codebase refers to the judge batch's *runtime* mode (`--judge` vs `--judge-apply`),
   not to a rehearsal of the installer. The rehearsal is the backup in step 6.

   ```bash
   cd ~/ccc-node && git pull origin main
   ./scripts/install-nunchi.sh --apply --claude --judge-apply
   ```

8. **Verify the record, then the artifact** — in that order:

   ```bash
   python3 -c "import json,os;d=json.load(open(os.path.expanduser('~/.claude/state/install-nunchi.json')));print(d['argv'], d['gen'], d['applied_at'])"
   crontab -l | grep 'nunchi:#816'
   ```

   `argv` contains the new flag, `applied_at` is fresh, `gen` matches the current
   installer hash, and the cron line is present with the expected env
   (e.g. `NUNCHI_JUDGE_APPLY=1`). Record correct but artifact missing = the installer
   didn't apply; artifact correct but record stale = it will vanish on next replay.

# Safety

- **Enabling an apply-mode flag mutates a data store** — `--judge-apply` lets the
  judge batch write to the fact store. That is a fresh-approval, per-node decision;
  never add it as a default or roll it out beyond the nodes explicitly approved.
- Re-running rewrites managed cron lines. Snapshot the crontab first (step 6).
- Preserve *every* flag from the existing record. Dropping an unrelated flag
  (e.g. `--audience-scoped`) is the same silent-revert bug in a new place.
- Fix the installer (Part A) **before** syncing the fleet (Part B). Syncing first just
  re-records flags the next drift will drop again.

# Verification

- [ ] The flag appears in `record_argv` in the installer source.
- [ ] Implication between flags is resolved so replay reproduces the stronger mode.
- [ ] A replay test asserts the artifact survives a drifted `gen`.
- [ ] Each node's record backed up before re-run.
- [ ] Each node's `argv` contains the flag and `applied_at` is fresh.
- [ ] Each node's `gen` matches the current installer, so no immediate replay is queued.
- [ ] The enabled artifact (cron line / hook / service) is live on each node.
- [ ] No unrelated flag was dropped from any record.
