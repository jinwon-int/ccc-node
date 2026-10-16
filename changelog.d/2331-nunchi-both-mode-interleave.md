- **fix(nunchi): `CCC_NUNCHI_VERBATIM_BACKEND=both` no longer starves gotgan (#2331 P3).**
  The first cut appended gotgan excerpts after MemPalace's and then cut the
  merged list at 40 lines; MemPalace alone fills 40 lines on most queries, so a
  `both` shadow run was silently a MemPalace run (shadow-node smoke 2026-10-16:
  40 MemPalace / 0 gotgan lines). Each backend now keeps its own 20-line share
  and the two are interleaved, so a flooding backend cannot push the other out.
  Default backend is still `mempalace`; `gotgan`-only output is unchanged.
