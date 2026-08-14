---
name: bilingual-terminology-review-gate
description: Systematically review bilingual medical terminology pairs in multilingual clinical content (NCLEX, etc.) across all changed keys, validating semantic equivalence, clinical accuracy, and standards compliance, then render structured PASS/BLOCK verdict.
metadata:
  type: claude
---

## When to Use

When reviewing A2AD `terminology_bilingual` lanes for NCLEX or similar multilingual clinical content, where every changed `TERM-REC` key must be individually validated and accounted for before screen→reviewed promotion. Use when a terminology review verdict must justify every changed key with clinical/linguistic evidence, risk flagging, and standards cross-reference.

## Procedure

1. **Preparation**:
   - Receive payload.json with `sourceBundle.files[].content` containing `changedRecords: [{ key, pair, record }]`
   - Establish standards baseline: KMA edition (e.g., 6.1), OpenRN text editions (Health Alterations, Fundamentals, Skills), clinical practice guidelines
   - Parse `intentContract.invariants` (e.g., "account for all N changed keys exactly once", "missing/duplicated/mistranslated term returns BLOCK")

2. **Per-key validation loop** (for each TERM-REC key):
   - **Semantic equivalence**: EN/KO phrase pair for idea-for-idea match; flag if KO adds, drops, or reinterprets concepts
   - **Clinical term accuracy**: Cross-reference each medical term (stroke, dysphagia, aspiration pneumonia, neurologic, etc.) against KMA preferred/accepted list and OpenRN source text edition/page
   - **Numerics and units**: Verify numbers (% SpO₂, angles, etc.) and unit names (IV→정맥, not transliteration) match EN exactly
   - **Forbidden-word enforcement**: Exclude discouraged terms (e.g., 中風 for stroke) if listed in baseline standard
   - **Abbreviation expansion**: Ensure first-use abbreviations are spelled out in KO; no abbreviation-only translations allowed
   - **Nesting context**: If a term appears in nested fields (phases[].body, items[].options[].why), confirm all instances use the same approved KO term

3. **Evidence tethering**:
   - For each clinical term: cite KMA entry (version, edition, retrievedAt, exactMatchCount)
   - Link clinical rationale to OpenRN pages (§ number, edition, page range) or hospital policy document
   - Record diffHash/intentHash agreement for payload integrity

4. **Structured findings**:
   - Per-key entry: `TERM-REC <key> PASS: <field>, <EN term>→<KO term> <status>, <clinical ref>`
   - Aggregate: total keys, error keys (expect 0), risks, non-blocking recommendations
   - Example non-blocking flag: "'plain water'→'생수' is colloquial; consider '일반 물' for precision with nectar-thick contrast"

5. **Verdict issuance**:
   - **PASS**: All N changed keys accounted for, 0 critical errors, 0 concept mismatches, semantic equivalence confirmed, clinical terms standards-aligned
   - **BLOCK**: Any key missing, duplicated, mistranslated, abbreviation-only, or unsupported medical term; OR semantic drift affecting clinical safety

6. **Risk and next-action**:
   - If sourceOnly=true: note "GitHub live diff not performed; re-examine headSha against actual PR if needed"
   - If verdict is PASS: affirm that PASS does not authorize merge, does not count as RN licensure review, does not affect source.verified status
   - Identify parallel lanes (high_risk_safety, content_clinical, evidence_adversarial) still pending and required for promotion

## Safety

- **Clinical accuracy is binding**: Terminology defects mislead clinicians. Never waive confirmed medical-term mismatch or semantic drift.
- **Invariant adherence**: Contract requirements (e.g., "all N changed keys exactly once") are binding; violation→BLOCK.
- **Scope clarity**: This lane validates terminology only; it does NOT approve merge, does NOT substitute for RN licensure review, does NOT grant paid-pool eligibility. Document in verdict.
- **Standards baseline**: Always cite specific KMA edition and OpenRN edition with timestamp. Do not drift to colloquial or local dialect unless explicitly approved.

## Verification

- **Self-check before PASS**:
  - [ ] Counted exactly N TERM-REC keys (the contract-declared count); zero missing or duplicated
  - [ ] Every key has EN/KO pair with semantic equivalence confirmed
  - [ ] Every medical term has KMA reference with exactMatchCount > 0
  - [ ] No forbidden terms; all abbreviations expanded in KO
  - [ ] All numerics and units match EN exactly
  - [ ] Invariants documented and affirmed in verdict
  
- **Post-PASS**:
  - Confirm PASS does not auto-promote screen→reviewed; parallel lanes must complete
  - If sourceOnly=true, flag: "GitHub live diff re-validation recommended before final merge"
  - Link PASS verdict to claim task ID (e.g., RNM-20260810-008) for audit trail
