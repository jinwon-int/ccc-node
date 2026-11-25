- **nunchi:** retire the ten-minute `jev-review.py` cron line (#2905). New
  `scripts/retire-nunchi-jev-review-cron.sh` comments the hand-installed line
  out behind a dated marker (dry-run by default, private crontab backup,
  `--restore --apply` rollback, `--status`), leaving `jev-review.db` and the log
  as evidence. A fleet measurement found about 9,300 runs per node and three
  Jev attempts in total. `docs/memory-pipeline-repair.md` now says not to
  install the schedule.
