- **deps(security): urllib3 2.7.0 → 2.8.0 and oauthlib 3.3.1 → 4.0.0 (#1864
  tracker; Dependabot #5–#12).** `.github/requirements/bridge-ci.txt` (CI
  hash lock, via requests) and `scripts/termux-mempalace-requirements.txt`
  take the first patched versions for GHSA-vxq7-64xx-v4gw, GHSA-8988-9cw3-xx77,
  GHSA-gh4c-6fx4-qh6g (urllib3) and GHSA-hj66-6f7g-4r5v, GHSA-xpv3-w29h-x7cv
  (oauthlib). The four chromadb alerts (#1–#4) still have no patched release
  and stay with #1864's release watch.
