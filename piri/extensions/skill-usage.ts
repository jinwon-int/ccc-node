/**
 * Piri adapter for the shared skill-use ledger (#1692 B).
 *
 * A paired read tool_call/tool_execution_end with isError === false records
 * successful read evidence. An input matching a listed /skill:name command
 * records an invocation attempt; input precedes expansion, which may fail or
 * be intercepted. Availability/listing alone is never evidence of use.
 *
 * The existing logger receives its Read/Skill stdin contract with only a skill
 * name and a synthetic /skills/<name>/SKILL.md path, never the original path,
 * prompt, or tool result. Its ts/skill/tool ledger format stays unchanged.
 * Existing flock and owner-only ledger permissions are reused; the logger has
 * no retention policy. Its aggregate report does not separate these semantics
 * or identify which runtime supplied a line.
 *
 * pi.exec has no stdin. Dedicated process groups provide bounded, best-effort
 * children instead: at most four active loggers, no queued overflow, timeout
 * kills descendants (including inherited flock descriptors). Events never
 * await logging. Missing/unsupported/malformed events fail open.
 */

import { spawn } from "node:child_process";
import * as fs from "node:fs";
import type {
	ExtensionAPI,
	InputEvent,
	ToolCallEvent,
	ToolExecutionEndEvent,
} from "@earendil-works/pi-coding-agent";

const SKILL_COMMAND_PREFIX = "/skill:";
/** Bounded child: pi must never wait longer than this on the logger. */
const DEFAULT_TIMEOUT_MS = 4000;
const MIN_TIMEOUT_MS = 50;
const MAX_TIMEOUT_MS = 30000;
/** Stdin budget the logger itself already enforces (64 KiB) — stay under it. */
const MAX_PAYLOAD_BYTES = 8192;
/** Reads in flight, bounded so a pathological session cannot grow this map. */
const MAX_PENDING_READS = 256;
/** Drop excess telemetry instead of queueing unbounded logger processes. */
const MAX_LOGGER_CHILDREN = 4;
let activeLoggerChildren = 0;
const SKILL_NAME = /^[a-z0-9][a-z0-9-]{0,63}$/;
/** Matches exactly what skill-usage-log.sh accepts for the Read path. */
const SKILL_DOC_PATH = /(?:^|\/)skills\/([a-z0-9][a-z0-9-]{0,63})\/SKILL\.md(?:#[^\r\n]*)?$/;

/** Result of one bounded logger invocation (test/observability only). */
export interface EmitResult {
	delivered: boolean;
	timedOut: boolean;
}

/**
 * Skill name from a `/skill:` input, before pi expands it.
 * Mirrors agent-session's `_expandSkillCommand`: everything after the prefix
 * up to the first space. Returns null when there is no usable name.
 */
export function skillNameFromCommand(text: unknown): string | null {
	if (typeof text !== "string" || !text.startsWith(SKILL_COMMAND_PREFIX)) {
		return null;
	}
	const rest = text.slice(SKILL_COMMAND_PREFIX.length);
	const name = rest.split(" ", 1)[0] ?? "";
	return SKILL_NAME.test(name) ? name : null;
}

/** True only for paths skill-usage-log.sh would accept (Read of a SKILL.md). */
export function isSkillDocumentPath(path: unknown): boolean {
	if (typeof path !== "string" || path.length === 0 || Buffer.byteLength(path, "utf8") > MAX_PAYLOAD_BYTES) return false;
	return SKILL_DOC_PATH.test(path);
}

/** PostToolUse-shaped JSON for a successful SKILL.md read (no prompt bodies). */
export function payloadForRead(path: string): string {
	const name = SKILL_DOC_PATH.exec(path)?.[1];
	if (!name) throw new Error("not a supported skill document");
	// The logger needs only the skill name: never forward the original path.
	return JSON.stringify({ tool_name: "Read", tool_input: { file_path: `/skills/${name}/SKILL.md` } });
}

/** PostToolUse-shaped JSON for an explicit /skill:name invocation attempt. */
export function payloadForSkill(name: string): string {
	return JSON.stringify({ tool_name: "Skill", tool_input: { skill: name } });
}

function isRegularFile(path: string): boolean {
	try {
		return fs.lstatSync(path).isFile();
	} catch {
		return false;
	}
}

/**
 * Locate the shared logger at call time so runtime HOME / CCC_CLAUDE_DIR are
 * honored (never baked in at module load). Order: explicit override, then the
 * Claude harness dir, then the HOME default. Null = no logger, emit nothing.
 */
export function resolveLoggerScript(
	env: NodeJS.ProcessEnv = process.env,
): string | null {
	const override = env.CCC_SKILL_USAGE_LOGGER;
	if (typeof override === "string" && override.length > 0) {
		return isRegularFile(override) ? override : null;
	}
	const claudeDir = env.CCC_CLAUDE_DIR;
	if (typeof claudeDir === "string" && claudeDir.length > 0) {
		const candidate = `${claudeDir}/hooks/skill-usage-log.sh`;
		if (isRegularFile(candidate)) return candidate;
	}
	const home = env.HOME;
	if (typeof home === "string" && home.length > 0) {
		const candidate = `${home}/.claude/hooks/skill-usage-log.sh`;
		if (isRegularFile(candidate)) return candidate;
	}
	return null;
}

function boundedTimeoutMs(env: NodeJS.ProcessEnv): number {
	const raw = env.CCC_SKILL_USAGE_LOG_TIMEOUT_MS;
	if (typeof raw !== "string" || raw.length === 0) return DEFAULT_TIMEOUT_MS;
	const parsed = Number.parseInt(raw, 10);
	if (!Number.isFinite(parsed)) return DEFAULT_TIMEOUT_MS;
	return Math.min(Math.max(parsed, MIN_TIMEOUT_MS), MAX_TIMEOUT_MS);
}

/**
 * Feed one event to the logger as a bounded child process. Resolves even when
 * the child hangs (timeout kill), cannot spawn, or exits nonzero — callers
 * fire-and-forget. Deliberately does NOT use pi.exec: it cannot carry stdin.
 */
export function emitSkillUse(
	loggerScript: string,
	payload: string,
	options?: { timeoutMs?: number; env?: NodeJS.ProcessEnv },
): Promise<EmitResult> {
	const env = options?.env ?? process.env;
	const requested = options?.timeoutMs ?? boundedTimeoutMs(env);
	const timeoutMs = Number.isFinite(requested)
		? Math.min(Math.max(requested, MIN_TIMEOUT_MS), MAX_TIMEOUT_MS)
		: DEFAULT_TIMEOUT_MS;
	const bytes = Buffer.from(`${payload}\n`, "utf8");
	if (bytes.length > MAX_PAYLOAD_BYTES || activeLoggerChildren >= MAX_LOGGER_CHILDREN) {
		return Promise.resolve({ delivered: false, timedOut: false });
	}
	return new Promise<EmitResult>((resolve) => {
		let settled = false;
		let timedOut = false;
		let child: ReturnType<typeof spawn>;
		activeLoggerChildren += 1;
		try {
			child = spawn("bash", [loggerScript, "log"], {
				stdio: ["pipe", "ignore", "ignore"],
				// A separate process group lets timeout kill flock/sleep descendants,
				// including descendants that inherited the logger's lock descriptor.
				detached: true,
				env,
			});
		} catch {
			activeLoggerChildren -= 1;
			resolve({ delivered: false, timedOut: false });
			return;
		}
		const killGroup = () => {
			try {
				if (child.pid !== undefined) process.kill(-child.pid, "SIGKILL");
			} catch { /* already gone */ }
		};
		const finish = (delivered: boolean) => {
			if (settled) return;
			settled = true;
			clearTimeout(timer);
			activeLoggerChildren -= 1;
			resolve({ delivered, timedOut });
		};
		const timer = setTimeout(() => {
			timedOut = true;
			killGroup();
			child.stdin?.destroy();
			finish(false);
		}, timeoutMs);
		child.on("error", () => finish(false));
		child.stdin?.on("error", () => {});
		child.on("close", (code) => {
			// A logger that exits while leaving background descendants is also
			// bounded; none may outlive this best-effort invocation.
			killGroup();
			finish(!timedOut && code === 0);
		});
		try {
			child.stdin?.end(bytes);
		} catch {
			killGroup();
			finish(false);
		}
	});
}

/** Narrow a tool_call event to the read tool without runtime package imports. */
function readPathFromToolCall(event: ToolCallEvent): string | null {
	const candidate = event as { toolName?: unknown; input?: { path?: unknown } };
	if (candidate.toolName !== "read") return null;
	const path = candidate.input?.path;
	return typeof path === "string" ? path : null;
}

/** True when pi lists a matching skill command; this does not prove expansion. */
export function skillCommandExists(
	pi: ExtensionAPI,
	name: string,
): boolean {
	try {
		const commands = pi.getCommands();
		if (!Array.isArray(commands)) return false;
		const wanted = `skill:${name}`;
		return commands.some(
			(command) =>
				command != null &&
				typeof command === "object" &&
				(command as { source?: unknown }).source === "skill" &&
				(command as { name?: unknown }).name === wanted,
		);
	} catch {
		return false; // unsupported API: fail open, log nothing
	}
}

export default function skillUsageExtension(pi: ExtensionAPI): void {
	// toolCallId -> SKILL.md path, captured at tool_call, consumed at
	// tool_execution_end. One entry per tool call, so one ledger line per read
	// even if pi later replays or reorders events.
	const pendingReads = new Map<string, string>();
	// Bound per-instance pending promises as well as the process-wide children.
	const inflight = new Set<Promise<EmitResult>>();
	// The missing-override note fires at most once per process: a stderr line on
	// every event would be its own kind of telemetry spam.
	let overrideWarned = false;

	const emit = (payload: string): void => {
		if (inflight.size >= MAX_LOGGER_CHILDREN) return;
		try {
			const loggerScript = resolveLoggerScript();
			if (loggerScript === null) {
				// No logger on this node: stay silent (best-effort contract). An
				// explicit override that resolves to nothing is worth one bounded
				// stderr note — silent total telemetry loss is how #1692 happened.
				const override = process.env.CCC_SKILL_USAGE_LOGGER;
				if (typeof override === "string" && override.length > 0 && !overrideWarned) {
					overrideWarned = true;
					console.error(
						"[skill-usage] CCC_SKILL_USAGE_LOGGER is set but does not resolve to a file; ledger disabled",
					);
				}
				return;
			}
			const promise = emitSkillUse(loggerScript, payload);
			inflight.add(promise);
			void promise
				.catch(() => ({ delivered: false, timedOut: false }))
				.finally(() => {
					inflight.delete(promise);
				});
		} catch {
			/* fail open */
		}
	};

	pi.on("tool_call", (event: ToolCallEvent) => {
		try {
			const path = readPathFromToolCall(event);
			if (path === null || !isSkillDocumentPath(path)) return;
			const toolCallId = (event as { toolCallId?: unknown }).toolCallId;
			if (typeof toolCallId !== "string" || toolCallId.length === 0 || toolCallId.length > 256) return;
			if (pendingReads.size >= MAX_PENDING_READS) {
				// Bounded memory: drop the oldest in-flight read rather than grow.
				const oldest = pendingReads.keys().next();
				if (oldest.done !== true) pendingReads.delete(oldest.value);
			}
			pendingReads.set(toolCallId, path);
		} catch {
			/* fail open */
		}
	});

	pi.on("tool_execution_end", (event: ToolExecutionEndEvent) => {
		try {
			const toolCallId = (event as { toolCallId?: unknown }).toolCallId;
			const path =
				typeof toolCallId === "string" ? pendingReads.get(toolCallId) : undefined;
			if (typeof toolCallId === "string") pendingReads.delete(toolCallId);
			const toolName = (event as { toolName?: unknown }).toolName;
			if (toolName !== "read" || path === undefined) return;
			const isError = (event as { isError?: unknown }).isError;
			if (isError !== false) return; // only an explicit successful result is evidence
			emit(payloadForRead(path));
		} catch {
			/* fail open */
		}
	});

	pi.on("input", (event: InputEvent) => {
		try {
			const text = (event as { text?: unknown }).text;
			const name = skillNameFromCommand(text);
			if (name === null) return;
			// Input precedes expansion and later handlers may transform/handle it.
			// Record only the named invocation attempt, never proven expansion.
			if (!skillCommandExists(pi, name)) return;
			emit(payloadForSkill(name));
		} catch {
			/* fail open */
		}
	});

	pi.on("session_shutdown", () => {
		pendingReads.clear();
	});
}
