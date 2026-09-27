- **Skills: the listing policy now fits recently used skills into the budget,
  and the core descriptions are shorter (#2031).** After #2011 A five nodes
  still estimated 17,511–21,583 listing chars against a 16,000 budget: the
  core skills' descriptions alone cost ~7,700 chars, and every skill used in
  the last 30 days kept its description regardless of size.
  `ccc-skill-listing-policy.py` now pays fixed costs first (operator entries,
  core descriptions, name-only entries), then describes recent skills by most
  recent use (then use count, then name) while the estimate stays within the
  budget; the rest become policy-owned `name-only` with reason `recent, over
  budget`, and lose that entry again once they fit. Ownership, "never off",
  and idempotence are unchanged. `plan` / `plan --json` report
  `after.over_budget`, a described/core/name-only breakdown, and a `WARNING`
  naming the dominant cost (core descriptions vs name list) when the fixed
  costs alone exceed the budget. New env `CCC_SKILL_LISTING_CONTEXT_TOKENS`
  (default 200000) for nodes on a larger context window. The 16 repo-shipped
  core skills' descriptions now lead with "Use when …" and are ≤300 chars
  (5,200 instead of 7,664 listing chars for the 17 measured core skills); a
  test enforces the 350-char cap and trigger-first wording.
