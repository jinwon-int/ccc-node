- **ccc-memory-check / ccc-doctor: stop flagging the Claude audience-scoped
  nunchi lane for the MemPalace refresh it deliberately lacks (#1921
  follow-up).** `install-nunchi.sh --claude --audience-scoped` never wires the
  verbatim refresh cron, but the memory probe demanded exactly one whenever the
  mempalace CLI was installed, so a correctly configured node reported
  `nunchi=degraded` (`refresh-count`) and the doctor's `memory cache` row
  warned. The probe now derives that lane from the managed cron (claude feed +
  `CCC_NUNCHI_AUDIENCE_SCOPED`), reports `nunchi.cron.refresh_contract:
  absent-by-design` and `mempalace: optional`, and no longer judges per-scope
  refresh status files a previous Piri lane left behind (they only age). The
  doctor's `nunchi collection` row likewise skips leftover refresh status and
  keeps aging the top-level ingest tick instead of a stale per-scope one. A
  refresh or legacy sweep line on this lane is still flagged
  (`refresh-unexpected` / `legacy-sweep`, doctor `refresh-cron-unexpected`);
  Piri scoped and non-scoped Claude lanes are unchanged.
