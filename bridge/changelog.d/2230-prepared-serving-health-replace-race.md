- **Prepared restart no longer refuses a healthy bridge when a health heartbeat
  lands mid-read (#2230).** The serving-generation check rejected `health.json`
  whenever the bot's atomic `os.replace()` swapped it between `open()` and
  `fstat()` (the opened inode then has zero links), so the single-shot pre-stop
  and post-recovery checks failed at random with `serving_generation_unverified`
  (restart rc 6, or 8 after a recovery). Only hardlinked aliases are rejected
  now; owner, mode, regular-file and freshness checks are unchanged.
