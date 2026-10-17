- **skill-promotion: a superseded promote batch the publisher itself marked
  ready is auto-closed again (#2360).** `_supersede_promote_prs` (#2011)
  only auto-closed "untouched" batches — a draft with the generator's single
  commit — but `_sweep_promote_merge` runs `gh pr ready` on every green batch,
  so by the time a newer batch superseded it the old PR was never a draft and
  was reported as human-edited and left open (fleet-skills #464, #470, #493,
  #494, #495 were closed by hand). The merge sweep now records an
  `a2a-promote-ready` ledger row with the head it readied, and a batch counts
  as untouched when it has one commit and is either still a draft or still at
  that recorded head. A human ready-mark (no row), any later push or a
  force-push (head moved) still leaves the PR open; a failed row write falls
  back to the same safe "human-edited" answer.
