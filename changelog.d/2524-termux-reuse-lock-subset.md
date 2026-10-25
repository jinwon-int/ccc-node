- **Termux prepare: native-wheel reuse now keys off the native/build-tool lock
  subsets, not the whole lock files (#2524).** A pure-Python bump (#2433
  certifi) changed `bridge/requirements.lock.txt`, so both the
  `termux-prepare-generation.sh` whole-file `git diff` prefilter and
  `termux_prepare.py`'s `lock_sha256` match refused reuse; gongyung then timed
  out on the full native build every tick from 2026-10-22 to 2026-10-26 even
  though its `native.lock.txt` / `build-tools.lock.txt` were byte-identical
  between the two jobs. The receipt now records `lock_subset_sha256` (SHA-256
  of exactly what `lock_subset()` writes: pins plus `--hash` entries of the
  NATIVE and BUILD_TOOLS packages) and `REUSE_MATCH_FIELDS` matches on it;
  `lock_sha256` stays as evidence. A pre-#2524 ready receipt is matched via the
  prior job's own subset files (regular, non-symlink, owner-private, bounded;
  else `reuse_lock_subset_unavailable`). The parser moved to the stdlib-only
  `bridge/termux_lock_subset.py`; the generation script pipes the target's copy
  into `python3 -I -B - --compare-git` as its prefilter (`lock_subsets=equal|
  different|undecided:<why>` in the dry-run/building lines; undecided → no
  reuse). A `reuse_*_mismatch` / `reuse_lock_subset_unavailable` refusal keeps
  the refused dir and retries once as a full build with the remaining budget.
  Other invariants (ready receipt only, toolchain/API/linker match, per-wheel
  digests, `reused_wheels_not_used`) are unchanged.
