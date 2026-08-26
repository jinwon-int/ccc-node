---
name: github-metered-billing-leak-diagnosis
description: Trace an unexplained GitHub metered-billing charge (GHAS/Secret Protection, Actions minutes) to the exact product, SKU, repo and day; separate gross from net, reconcile the meter against entitlement APIs and the payment receipt, and stop the bleed. Use when an org's GitHub bill is higher than expected, when a billing alert fires, when asked where a charge comes from or whether a feature is still being billed, or when deciding between Team and Enterprise on Actions minutes.
---

# GitHub metered-billing leak diagnosis

Scope: **GitHub org metered billing only.** Not Anthropic/token cost.

The happy path is three API calls. The value of this skill is the five traps
below — each was independently re-derived across several separate
investigations before being written down.

## The five traps

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

**5. `budget_amount` is licenses, not dollars — and `ghas` is not a usable
budget SKU.** Three separate rejections hide here, and the error text is the
only documentation that resolves them:
- The stop flag is `prevent_further_usage`, **not**
  `stop_usage_when_budget_exhausted`.
- `budget_type=ProductPricing` + `budget_product_sku=ghas` returns **500**:
  *"High watermark products cannot have PreventFurtherUsage budget alerting
  except for GHAS SKUs."* Read that as permission, not refusal — GHAS is the
  documented exception, but only at **`SkuPricing`** granularity.
- For license-based products `budget_amount` is **the number of licenses**,
  not a dollar cap. `budget_amount: 0` means *zero seats allowed*, not
  *$0 of spend*. (GitHub's own parameter doc: "The budget amount in whole
  dollars. For license-based products, this represents the number of
  licenses.")

**The error message is the SKU catalog.** The valid-SKU list is not worth
guessing at. Post a deliberately invalid SKU and the API enumerates all of
them:
```bash
gh api --method POST "/organizations/$ORG/settings/billing/budgets" \
  -f budget_type=SkuPricing -f budget_product_sku=zzz \
  -f budget_scope=organization 2>&1 | tr ',' '\n'
# → SKU 'zzz' not found. Available SKUs: actions_cache_storage, actions_linux,
#   ... ghas_code_security_licenses, ghas_secret_protection_licenses, ...
```
This POST is rejected at validation, so it creates nothing — but it *is* a
POST, not a GET. Treat it as the one probe that leaves the read-only path.

A sixth, milder trap for Actions specifically: **net spend is a lagging
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
Disable at the repo level first. If charges persist, set a hard stop.

Check what already exists before adding anything:
```bash
gh api "/organizations/$ORG/settings/billing/budgets" \
  --jq '.budgets[]|"\(.budget_product_sku)\t\(.budget_type)\tamount=\(.budget_amount)\tstop=\(.prevent_further_usage)"'
```
GHAS bills as two separate license SKUs and **each needs its own budget** —
capping one leaves the other charging. The full body is required; a partial
body returns `400 Missing required fields`:
```bash
for sku in ghas_secret_protection_licenses ghas_code_security_licenses; do
  jq -nc --arg sku "$sku" '{budget_amount:0, prevent_further_usage:true,
    budget_scope:"organization", budget_entity_name:"",
    budget_type:"SkuPricing", budget_product_sku:$sku,
    budget_alerting:{will_alert:false, alert_recipients:[]}}' \
  | gh api --method POST "/organizations/$ORG/settings/billing/budgets" --input -
done
```
`budget_amount:0` here means **zero licenses**, not zero dollars (trap 5). With
`prevent_further_usage:true` an org already over zero seats is blocked
immediately, not merely alerted.

Then re-run step 3 the next day. The toggle is not the proof; the missing row
is.

## Safety
- **Read-only by default.** Steps 1–6 are `gh api` GETs. Only step 7 mutates
  (plus the SKU-enumeration probe in trap 5, a POST that fails validation and
  creates nothing). A `budget_amount:0` hard stop on a license SKU means **zero
  seats**, so it blocks the feature outright for everyone — get owner approval
  before setting it, and note that removing the budget is what restores use.
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
