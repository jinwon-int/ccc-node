- **One rendering grammar for session trajectories; render cuts are always
  marked (#2388).** New `claude/hooks/lib/trajectory_render.py` turns Claude,
  Codex and Piri sessions into one grammar (`[USER]`, `[ASSISTANT]`,
  `[ACTION] tool(args)`, `[OBSERVATION]`, `[OBSERVATION ERROR]`, `[ERROR]`).
  Each cut it makes leaves `…(+N chars — render truncation)`, and the prompts
  of distill, skill-review and the nunchi codex/piri feeds now say that this
  marker comes from the renderer and does not mean an agent failure.
  `obs_trunc` must be omitted or ≥ 10,000; a smaller value is a configuration
  error. Callers set all budgets, and the module has no defaults. The old
  `...[truncated N bytes]...` header, the `[tool:X]`/`[tool_result:id]`
  placeholders and nunchi's unmarked 800/40,000-char cuts are removed.
  skill-review now sees commands (`[ACTION] Bash(<cmd>)`, up to 400 chars,
  redacted); distill still omits them. The Codex/Piri skill-autosave
  normalizers are now thin wrappers over the shared adapters. Their
  projection is byte-identical on 123 local sessions; headless Codex
  `event_msg` fallback rows now stay in session order.
