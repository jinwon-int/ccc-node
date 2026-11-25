- **nunchi: `merge-proposals` folds the judge report's duplicate proposals in one pass (#2910).**
  judge-batch holds a flagged fact with a near-identical open sibling out of
  the judge and writes `nunchi.py merge <dup> --into <survivor>` into the review
  report for the owner. The report shows ten lines and every fold was a
  separate command. On 2026-11-26 the fleet review queue held 277 items; 110
  were these proposals, 58 on one node, and the hourly judge had nothing
  eligible on any of the twelve nodes.
  `nunchi.py merge-proposals` is a dry run by default. It takes the pairs and
  their direction from judge-batch's `fetch_queue` (the code that writes the
  report) and adds one guard: a duplicate that outranks its survivor is held
  back and listed. `--apply` takes the judge lock, backs the store up, and
  folds every pair in one transaction with the same write as `merge` — the
  duplicate is closed, both rows get the evidence marker, no text changes and
  nothing is deleted. Rows that are no longer both open are skipped and
  counted. Review flags are not written: a flagged duplicate leaves the queue
  by closing and a flagged survivor is cleared by the next judge-batch run.
  One body-free audit row records the ids. `--limit N`, `--sample N` and
  `--json` shape the run; a second run is a no-op.
  `merge` now shares its row writes with the new command (`_merge_rows`), with
  the same messages and exit codes as before. `curation-authority.test.sh`
  fails if either path writes anything but the two merge writes.
