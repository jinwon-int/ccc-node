# A2A broker worker watch

`scripts/a2a-broker-worker-watch.py` pages when an A2A broker has not heard
from a worker for too long (#2086). It is the second, SSH-independent source
of fleet evidence next to [`fleet-watch.md`](fleet-watch.md): it reads the
broker's own `GET /workers` view, so it notices a node whose worker cannot
reach the broker (for example a node that lost DNS) even when nothing else on
the node looks wrong.

Run it as an agent-cron **command** task every 5 minutes on the broker host.
It only reads: one `GET /workers` (edge secret) and one public `GET /livez`
per broker per run. It never touches workers, tasks or the broker.

## Output and exit codes

Paging lines use the fleet tokens that agent-cron counts in its alert title
(`scripts/agent_cron.py` `fleet_diagnostic_title`), node first:

```text
DOWN <node> source=broker:<name> reason=worker-stale age=37m
DOWN <node> source=broker:<name> reason=worker-missing age=20m
UNREACHABLE broker:<name> reason=timeout age=10m runs=2
DEGRADED broker:<name> reason=auth-rejected age=10m runs=2
DEGRADED broker:<name> reason=mass-stale stale=4/5 nodes=a,b,c,d age=20m
UNVERIFIED broker:<name> reason=env-unreadable
```

Paging lines come first, then non-paging context (`ONGOING`, `PENDING`,
`RECOVERED`) and one `SUMMARY` line. None of these context prefixes is a fleet
token, so the title counts only the new pages.

| Exit | Meaning |
|---|---|
| `0` | Nothing to page (all online, below threshold, or a known finding not yet due to re-page). |
| `1` | At least one paging line (or a recovery with `--report-recovery`). |
| `2` | Usage or configuration error: bad flags, unreadable env file, `A2A_EDGE_SECRET` missing, state not writable. |

Use `--notify telegram-owner-on-failure` and do **not** set
`--success-exit-codes 0,1`: exit 1 must count as a failure, or the page is
never sent. Re-paging is handled by the script (exit 0 between pages), so the
task does not alert every 5 minutes.

The output carries node ids, broker names, reasons and ages only. The edge
secret, request headers and raw broker payloads are never printed. Node ids
from the broker outside `[A-Za-z0-9_.-]` are replaced by `node-<hash>`.

## Paging rules

- **Threshold.** A node pages after it has been non-online (`stale`, or
  missing from the list after this watch saw it online) continuously for
  `--threshold` (default `15m`), measured by this watch's own observations.
  The printed `age` is the longer of that and the broker's `lastSeenAt` age.
  On SQLite brokers `lastSeenAt` is persisted on a throttle (60s in
  production), so allow up to about a minute of lag.
- **Re-page.** While a node stays down it pages again on `--realert`
  (default `1h,6h`: one hour after the first page, then every 6 hours).
  The runs in between print `ONGOING ... next_alert_in=...` and exit 0.
- **Recovery.** A paged node that comes back prints `RECOVERED <node>
  down=...`. That only pages (exit 1) with `--report-recovery`.
- **Broker failures.** A broker that cannot be queried (DNS, refused,
  timeout, TLS, HTTP 5xx → `UNREACHABLE`; 401/403, other HTTP codes, bad
  payload → `DEGRADED`) is one broker finding after `--broker-fail-runs`
  consecutive failed runs (default 2). Worker state is frozen while the
  broker is unavailable, so an outage never marks every worker stale.
- **Restart grace.** For `--restart-grace` (default `10m`) after a broker
  restart (`/livez` `uptimeSec`, or `draining: true`), after the broker
  answers again following failed runs, or after a *mass flip* (at least
  `--mass-min` workers and `--mass-ratio` of the online ones going non-online
  in one run), workers that went non-online inside the window start their
  clock at its end.
- **Mass stale.** If at least `--mass-min` (default 3) and `--mass-ratio`
  (default 0.8) of the tracked workers are past the threshold at once, one
  `DEGRADED broker:<name> reason=mass-stale` line replaces the per-node lines.
  That usually points at the broker host or its network.
- **Exclusions.** `--exclude a,b` (repeatable) and
  `A2A_WORKER_WATCH_EXCLUDE=a,b` skip nodes that are offline by design, such
  as a phone worker. Excluded nodes are counted in `SUMMARY` only.
- **Leftover identities.** A worker this watch never saw online whose broker
  `lastSeenAt` is older than `--abandoned-after` (default `7d`) is ignored
  (canary or smoke identities). A node seen online is never ignored this way.

## Configuration

| Flag | Env | Default |
|---|---|---|
| `--broker NAME=URL` (repeatable) | `A2A_WORKER_WATCH_BROKERS=n1=url1,n2=url2` | required |
| `--edge-env-file PATH` | `A2A_WORKER_WATCH_EDGE_ENV` | `/root/.a2a-broker-edge.env` |
| `--broker-env-file NAME=PATH` (repeatable) | — | the edge env file |
| `--state-file PATH` | `A2A_WORKER_WATCH_STATE` | `$CCC_STATE_DIR/a2a-broker-worker-watch.json` (`~/.claude/state/...`) |
| `--exclude NODES` | `A2A_WORKER_WATCH_EXCLUDE` | none |
| `--threshold`, `--realert`, `--restart-grace`, `--abandoned-after`, `--timeout` | — | `15m`, `1h,6h`, `10m`, `7d`, `15s` |
| `--broker-fail-runs`, `--mass-min`, `--mass-ratio` | — | `2`, `3`, `0.8` |
| `--report-recovery` | — | off |

Durations accept `<n>`, `<n>s`, `<n>m`, `<n>h` or `<n>d`.

**Edge secret.** The env file must set `A2A_EDGE_SECRET` to the value the
broker checks (`EDGE_SECRET` in the broker container). It is sourced by
`bash` only, and the header is passed to `curl` through `--config -` on
stdin, so the value never enters Python, any process argv, or any output.
Keep the file `0600`. Because it is sourced, the file may also derive the
value at run time instead of storing a copy, for example on the broker host:

```sh
# /root/.a2a-broker-edge.env (0600) — reads the live broker secret each run
A2A_EDGE_SECRET=$(docker exec <broker-container> printenv EDGE_SECRET)
```

A stored copy that no longer matches the broker shows up as
`DEGRADED broker:<name> reason=auth-rejected`.

**State.** One small JSON file per watch, written atomically (temp file,
`fsync`, rename) with mode `0600` in a `0700` directory, plus a `.lock` file
so overlapping runs skip instead of racing. It records per broker: failed-run
streak, grace window, and per node the first non-online time and page
history. A corrupt or unknown-version file is reset with
`WARN state-reset reason=...` on stderr; the worst case is that a down node
waits one threshold again.

## Recommended agent-cron task (team2 broker host)

Do not add this until the operator approves it. Run from the installed
checkout on the broker host:

```sh
cd /root/ccc-node
scripts/agent-cron.sh add a2a-broker-worker-watch \
  --schedule 'every 5m' --timezone Asia/Seoul \
  --prompt 'A2A broker worker staleness watch (#2086)' \
  --notify telegram-owner-on-failure \
  --catch-up-policy skip --timeout-sec 120 \
  --argv python3 --argv /root/ccc-node/scripts/a2a-broker-worker-watch.py \
  --argv --broker --argv team2=http://127.0.0.1:8787 \
  --argv --edge-env-file --argv /root/.a2a-broker-edge.env
```

Append `--argv --exclude --argv <node>` for a worker that is offline by
design. Before adding the task, run the same `python3 ...` command once by
hand: it should print `SUMMARY brokers=1/1 ...` and exit 0. Each broker host
watches its own broker over `127.0.0.1`, so the check does not depend on
public DNS.

## Tests

`scripts/a2a-broker-worker-watch.test.sh` (discovered by
`validate-harness.sh`) runs a curl stub for secret transport, exit codes and
the agent-cron title, then the unit suite
`scripts/a2a_broker_worker_watch_test.py` (thresholds, re-page stages,
recovery, exclusions, broker failures, restart grace, mass stale, state).
