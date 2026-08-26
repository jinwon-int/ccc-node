---
name: github-metered-billing-leak-diagnosis
description: Trace an unexplained GitHub metered-billing charge (GHAS/Secret Protection, Actions minutes) to the exact product, SKU, repo and day; separate gross from net, reconcile the meter against entitlement APIs and the payment receipt, and stop the bleed. Use when an org's GitHub bill is higher than expected, when a billing alert fires, when asked where a charge comes from or whether a feature is still being billed, or when deciding between Team and Enterprise on Actions minutes.
---

# GitHub metered-billing leak diagnosis

Scope: **GitHub org metered billing only.** Not Anthropic/token cost.

The happy path is three API calls. The value of this skill is the four traps
below — each was independently re-derived across several separate
investigations before being written down.

## The four traps

**1. The legacy billing endpoints are dead.**
`/orgs/{org}/settings/billing/actions` returns 404, or `total_ms: 0`, which
reads like "no usage" instead of "wrong endpoint". Always use the enhanced
endpoint: `/organizations/{org}/settings/billing/usage`.

**2. Your node token almost certainly lacks billing scope.**
Billing APIs are **org-owner only**. A 403 here is expected, not a bug. Fall
back to the node that holds the owner credential over SSH (read-only `gh api`
GETs only — never read, print or copy the token itself).

**3. The entitlement API and the meter contradict each other.**
"Active repos: 0, active committers: 0" can coexist with the meter charging
every single day. **Verify by metered days, never by the feature toggle.** A
per-day amount of exactly `1/31` of a monthly unit price is the signature of a
seat/committer-month being amortised daily.

**4. A stopped charge produces a MISSING ROW, not a `$0` row.**
When metering stops, GitHub emits no line item for that day at all. Any watcher
that looks for "last charged day" alone will therefore report "still charging"
forever. **Freshness is the signal, not amount**: compare the newest charged
date against today and treat anything older than ~2 days as stopped.

A fifth, milder trap for Actions specifically: **net spend is a lagging
indicator.** It stays `$0` until the included tier is exhausted mid-month, then
jumps. Track *minutes*, not dollars, to see the cliff coming.

## Procedure

### 1. Pull the month's usage
```bash
ORG=<org>; Y=$(date +%Y); M=$(date +%-m)
gh api "/organizations/$ORG/settings/billing/usage?year=$Y&month=$M" > /tmp/usage.json
```
On 403, re-run the same call on the owner-credential host:
```bash
ssh -o BatchMode=yes "${BILLING_HOST:-<owner-host>}" \
  "gh api '/organizations/$ORG/settings/billing/usage?year=$Y&month=$M'" > /tmp/usage.json
```

### 2. Attribute by product × SKU × repo × day
Separate **gross** (what was metered) from **net** (what you pay after
included allowances). A large gross with small net is a free tier doing its
job; a large net is the real leak.
```bash
jq -r '[.usageItems[]|select(.product=="ghas")]
       | group_by(.sku + .repositoryName)[]
       | "\(.[0].sku)\t\(.[0].repositoryName)\t\(map(.netAmount)|add)"' /tmp/usage.json

jq -r '[.usageItems[]|select(.product=="actions" and .unitType=="Minutes")]
       | group_by(.repositoryName)[]
       | "\(.[0].repositoryName)\t\(map(.quantity)|add|round)분"' /tmp/usage.json
```

### 3. Check freshness, not amount (trap 4)
```bash
jq -r '[.usageItems[]|select(.product=="ghas")]
       | "last=\(.[-1].date[0:10]) amt=\(.[-1].netAmount) days=\(length)"' /tmp/usage.json
```
Compare `last` against today. Still within ~2 days → **still accruing**.
Frozen days ago → **stopped**, regardless of the amount shown.

### 4. Cross-check entitlement against the meter (trap 3)
```bash
gh api "/orgs/$ORG/settings/billing/advanced-security?per_page=100"   # active committers
for r in $(gh api "/orgs/$ORG/repos?per_page=100" --jq '.[]|select(.visibility!="public")|.name'); do
  gh api "/repos/$ORG/$r" --jq 'select((.security_and_analysis.secret_scanning.status=="enabled")
    or (.security_and_analysis.code_security.status=="enabled"))|"\(.name) \(.visibility)"'
done
```
**Public repos are free; private repos bill.** The usual root cause is new-repo
hardening that enables secret scanning / push protection without checking
visibility first. If the meter charges but this returns nothing, believe the
meter and escalate — do not close the investigation.

### 5. Confirm against ground truth
The API is the meter; the receipt is the invoice, and they can disagree. Read
the actual payment receipt, and pin unit prices from the published billing
docs rather than from memory — rates and included minutes change.

### 6. Project honestly
The usage report lags roughly one day. Projecting from today's day-of-month
therefore **understates** usage. Project from the number of days actually
covered by data:
```
projected = minutes_so_far * days_in_month / distinct_days_with_data
```

### 7. Stop the bleed, then verify by the meter
Disable at the repo level first. If charges persist, set a hard stop:
```bash
gh api -X POST "/organizations/$ORG/settings/billing/budgets" \
  -f budget_amount=0 -F prevent_further_usage=true
```
Then re-run step 3 the next day. The toggle is not the proof; the missing row
is.

## Safety
- **Read-only by default.** Steps 1–6 are `gh api` GETs. Only step 7 mutates,
  and a `budget_amount=0` hard stop can block real work — get owner approval
  before setting it.
- Never read, print, echo or copy the owner token. SSH runs `gh` remotely so
  the credential never leaves that host.
- Redact long opaque strings before pasting output anywhere.

## Verification
- The charge is attributed to a specific product, SKU, repo and day range.
- Gross and net are reported separately.
- The "still accruing vs stopped" verdict comes from date freshness (trap 4).
- Any entitlement/meter contradiction is stated explicitly rather than resolved
  in favour of whichever source is more convenient.
- After remediation, the newest charged date stops advancing on a later run.

## Related
A daily watcher implementing this method lives at
`~/.claude/scripts/ghas-billing-watch.sh` (report → `~/.claude/state/`,
owner push only when something is flagged). Read it before writing new
tooling; it already encodes all four traps.
