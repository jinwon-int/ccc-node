- **auto-distill: canon lookup passes the query after `--` and never pages
  (#2145).** `canon_snippets` ran `wiki-agent find <query>`, and the query
  starts with the candidate title — titles such as `--apply 렌더 이력 jsonl`
  were parsed as an unknown find option (exit 64). Each such run sent a
  `find user-input exit=64` failure alert, and the empty stdout came back as
  `[]` ("no canon match") instead of `None`, so the canon-duplicate check was
  silently skipped for that item. The call is now
  `wiki-agent --no-notify find -- <query>` and a nonzero exit returns `None`
  (`search_failed`). `test_canon_find.py` +5.
  Receipt re-issued as **TM-3657** (source `bf160226…`, surface `9894a32b…` →
  `be987eac…`, `canon_snippets` changed): TP 13 / FP 2 / FN 10 / TN 22
  (precision 87%, recall 57%), recheck 7/12 over baseline 1, collateral 0;
  frozen snapshot corpus `95582b2b…` (same as TM-3448), 47/47 envelopes
  `claude-haiku-4-5-20251001`.
