- **Single-file HTML reports under an `artifacts/` directory are deliverables (#2109 C).**
  `html`/`htm` stay off the general sendable list — an ordinary coding turn
  that says "I edited `public/index.html`" must not push web source to the
  chat — but a path with an `artifacts` directory component before the file
  name (e.g. `~/.claude/state/artifacts/fleet-matrix.html`) now matches the
  shared `FILE_PATH_RE` and is sent like any document, on Telegram and Matrix
  alike. The gate is in the regex (`ARTIFACT_ONLY_EXTENSIONS`,
  `ARTIFACT_DIR_NAME` in `core/deliverables.py`), so both frontends inherit
  it without further changes; the existing is_file/size/scope checks still
  apply. Pairs with the `fleet-html-report` skill, which writes its reports
  there.
