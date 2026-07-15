# MEMORY.md (per-node — durable operating memory)
# Save as /root/.hermes/memories/MEMORY.md. NEVER put raw secrets here — only credential locations / handling rules.

- This instance is <NODE_DISPLAY_NAME> / <NODE_NAME>. <PHYSICAL_SLOT> <FLEET_ROLE>. Treat A2A worker/model/gateway status as mutable and live-check before acting.
- Wiki-first Seoyoon Family operating policy: consult Family Wiki first, then web/GitHub/session search; use `wiki-agent load/find/prefetch` for reads and `wiki-agent write-path/pr` for durable updates. Never store raw secrets, only credential locations/handling rules.
- Official node mappings, models, service status, ports, gateway/channel health, GitHub state, and cron state are mutable. Verify source text from Wiki before operational claims or changes.
- Canonical Wiki structure: pages/nodes/<name>/, pages/team/<name>/, pages/owners/, plus runbooks/services/incidents/decisions/archive/log.
- New Family Wiki log entries use `LOG-YYYYMMDD-<node>-<same-day-seq>` under `pages/log.md` `[LOG-00]`; never create a new global numeric `LOG-NNNN` ID or renumber old entries.
- A2A/Nexus: canonical repo jinwon-int/a2a-nexus; durable changes use PR-first + real broker-backed worker evidence.
- A2A fleet boundaries: T1 = Seoseo broker, T2 = Gwakga broker; persistent workers use private broker tunnels where configured.
- Supermemory is retired/legacy. Current memory stack: built-in MEMORY/USER + Honcho + Family Wiki + session_search.
- GitHub repo hygiene: operational repos under jinwon-int; older personal-account duplicates archived/marked legacy.

# <Add node-specific durable facts below>
