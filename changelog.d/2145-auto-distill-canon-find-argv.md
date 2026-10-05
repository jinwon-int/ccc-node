- **auto-distill: canon lookup passes the query after `--` and never pages
  (#2145).** `canon_snippets` ran `wiki-agent find <query>`, and the query
  starts with the candidate title — titles such as `--apply 렌더 이력 jsonl`
  were parsed as an unknown find option (exit 64). Each such run sent a
  `find user-input exit=64` failure alert, and the empty stdout came back as
  `[]` ("no canon match") instead of `None`, so the canon-duplicate check was
  silently skipped for that item. The call is now
  `wiki-agent --no-notify find -- <query>` and a nonzero exit returns `None`
  (`search_failed`). `test_canon_find.py` +5. `canon_snippets` is on the
  evaluation-receipt surface (#1262): this change needs a fresh exact-source
  evaluation and a reissued `evaluation-receipt.json` before it can install.
