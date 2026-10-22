- **Test-only: `trajectory_render.test.sh` no longer depends on the locale's
  sort order (#2453).** The tag-set assertion compares a `sort -u` result with
  a fixed string; under `en_US.UTF-8` `[OBSERVATION]` sorts before
  `[OBSERVATION ERROR]` and local `validate-harness.sh` reported one FAIL
  while CI (C locale) stayed green. The assertion now sorts with `LC_ALL=C`.
