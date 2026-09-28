- **Danso: every session now starts with the fleet merge policy, so Danso stops
  polling indefinitely for PR approvals.** Danso sessions had no merge skill in
  their lane and only a node-local autosaved "wait for the other account's
  approval" skill, so they requested seoseo-ai/jinon86 approval and polled
  without end. The bridge now installs `bridge/core/danso_fleet_rules.md` as
  `<danso HOME>/.pi/agent/AGENTS.md` at every start (Danso reads it at every
  session start in both memory modes): self-serve the cross-account approval
  through `approve-via-relay.sh` when the user asked to merge a PR opened in
  the current task (exact head, green, mergeable, self-reviewed diff), use the
  `gh-pr-flow-danso` skill (fleet-skills#329), bound waiting per phase (30 minutes
  for head CI, 30 after enqueue), and never wait on a human approval. Operator-written files, symlinks
  and group-writable directories are never overwritten. New env
  `CCC_DANSO_FLEET_RULES` (default `true`) (#2040).
