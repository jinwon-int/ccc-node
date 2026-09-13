/**
 * skill-usage.test.ts — isolated driver for the piri skill-usage extension
 * (#1692 B). Runs under `node --experimental-strip-types` — no pi runtime, no
 * provider, no network: the extension loads as a plain module and its event
 * handlers are driven with synthetic events against fixture loggers.
 * Fixtures and orchestration live in skill-usage.test.sh; this file is the
 * per-group engine. Usage: node ... skill-usage.test.ts <group>
 */

import * as fs from "node:fs";
import { spawnSync } from "node:child_process";
import * as os from "node:os";
import * as path from "node:path";
import skillUsageExtension, {
	emitSkillUse,
	isSkillDocumentPath,
	payloadForRead,
	payloadForSkill,
	resolveLoggerScript,
	skillCommandExists,
	skillNameFromCommand,
} from "./skill-usage.ts";

let passed = 0;
let failed = 0;

function ok(label: string, condition: boolean, detail?: unknown): void {
	if (condition) {
		passed += 1;
		console.log(`PASS ${label}`);
	} else {
		failed += 1;
		console.log(`FAIL ${label}${detail === undefined ? "" : ` — ${String(detail)}`}`);
	}
}

interface CommandInfo {
	name: string;
	source: string;
}

/** Minimal ExtensionAPI stand-in: records registrations and usage counters. */
function makePi(commands: CommandInfo[]) {
	const handlers = new Map<string, Array<(event: unknown) => unknown>>();
	const usage = { exec: 0, getCommands: 0 };
	const pi = {
		on(event: string, handler: (event: unknown) => unknown): void {
			const list = handlers.get(event) ?? [];
			list.push(handler);
			handlers.set(event, list);
		},
		getCommands(): CommandInfo[] {
			usage.getCommands += 1;
			return commands;
		},
		async exec(): Promise<{ stdout: string; stderr: string; code: number; killed: boolean }> {
			usage.exec += 1;
			return { stdout: "", stderr: "", code: 0, killed: false };
		},
	};
	return { pi, handlers, usage };
}

async function fire(
	handlers: Map<string, Array<(event: unknown) => unknown>>,
	event: string,
	payload: unknown,
): Promise<void> {
	for (const handler of handlers.get(event) ?? []) {
		await handler(payload);
	}
}

function linesOf(file: string): string[] {
	try {
		return fs
			.readFileSync(file, "utf8")
			.split("\n")
			.filter((line) => line.length > 0);
	} catch {
		return [];
	}
}

/** Poll until `want(probe())` holds or the deadline passes; last value wins. */
async function waitFor<T>(
	probe: () => T,
	want: (value: T) => boolean,
	deadlineMs = 4000,
): Promise<T> {
	const start = Date.now();
	let value = probe();
	while (!want(value) && Date.now() - start < deadlineMs) {
		await new Promise((resolve) => setTimeout(resolve, 25));
		value = probe();
	}
	return value;
}

const sleep = (ms: number): Promise<void> =>
	new Promise((resolve) => setTimeout(resolve, ms));

const SKILL_COMMANDS: CommandInfo[] = [
	{ name: "skill:gh-pr-flow", source: "skill" },
	{ name: "skill:web", source: "skill" },
	{ name: "doctor", source: "prompt" },
];

const readCall = (toolCallId: string, filePath: string) => ({
	type: "tool_call",
	toolCallId,
	toolName: "read",
	input: { path: filePath },
});

const readEnd = (toolCallId: string, isError: boolean) => ({
	type: "tool_execution_end",
	toolCallId,
	toolName: "read",
	isError,
});

// ---------------------------------------------------------------------------
// unit — pure helpers plus logger resolution in a fixture tree
// ---------------------------------------------------------------------------
async function groupUnit(): Promise<void> {
	ok("skillNameFromCommand: bare name", skillNameFromCommand("/skill:web") === "web");
	ok("skillNameFromCommand: args stripped", skillNameFromCommand("/skill:web fix it") === "web");
	ok("skillNameFromCommand: dashed name", skillNameFromCommand("/skill:gh-pr-flow") === "gh-pr-flow");
	ok("skillNameFromCommand: empty fragment", skillNameFromCommand("/skill:") === null);
	ok("skillNameFromCommand: blank name", skillNameFromCommand("/skill:   ") === null);
	ok("skillNameFromCommand: bare /skill", skillNameFromCommand("/skill") === null);
	ok("skillNameFromCommand: no prefix", skillNameFromCommand("skill:web") === null);
	ok("skillNameFromCommand: non-string", skillNameFromCommand(undefined) === null);
	for (const bad of ["/skill:WEB", "/skill:web\n", "/skill:web\t", "/skill:we_b", "/skill:web/other"]) {
		ok(`malformed skill identifier rejected ${JSON.stringify(bad)}`, skillNameFromCommand(bad) === null);
	}

	ok("isSkillDocumentPath: absolute", isSkillDocumentPath("/h/.claude/skills/web/SKILL.md"));
	ok("isSkillDocumentPath: root skills dir", isSkillDocumentPath("/skills/web/SKILL.md"));
	ok("isSkillDocumentPath: parented relative", isSkillDocumentPath("repo/skills/web/SKILL.md"));
	ok("isSkillDocumentPath: bare relative accepted", isSkillDocumentPath("skills/web/SKILL.md"));
	ok("isSkillDocumentPath: literal fragment filename rejected", !isSkillDocumentPath("/a/skills/web/SKILL.md#L5"));
	ok("isSkillDocumentPath: other doc rejected", !isSkillDocumentPath("/a/skills/web/README.md"));
	ok("isSkillDocumentPath: plural rejected", !isSkillDocumentPath("/a/skills/web/SKILLS.md"));
	ok(
		"isSkillDocumentPath: spaced+unicode",
		isSkillDocumentPath("/home/우주 노드/.claude/skills/gh-pr-flow/SKILL.md"),
	);
	ok("isSkillDocumentPath: non-string", !isSkillDocumentPath(42));
	ok("isSkillDocumentPath: empty", !isSkillDocumentPath(""));

	const readPayload = JSON.parse(
		payloadForRead("/a b/스킬/skills/web/SKILL.md"),
	) as Record<string, unknown>;
	const readInput = readPayload.tool_input as Record<string, unknown>;
	ok("payloadForRead: tool_name", readPayload.tool_name === "Read");
	ok("payloadForRead: file_path", readInput.file_path === "/skills/web/SKILL.md");
	const skillPayload = JSON.parse(payloadForSkill("web")) as Record<string, unknown>;
	const skillInput = skillPayload.tool_input as Record<string, unknown>;
	ok("payloadForSkill: tool_name", skillPayload.tool_name === "Skill");
	ok("payloadForSkill: skill name", skillInput.skill === "web");

	const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "skill-usage-unit-"));
	try {
		fs.mkdirSync(path.join(tmp, "claude/hooks"), { recursive: true });
		fs.writeFileSync(path.join(tmp, "claude/hooks/skill-usage-log.sh"), "#!/usr/bin/env bash\n");
		fs.mkdirSync(path.join(tmp, "home/.claude/hooks"), { recursive: true });
		fs.writeFileSync(path.join(tmp, "home/.claude/hooks/skill-usage-log.sh"), "#!/usr/bin/env bash\n");
		const logger = path.join(tmp, "claude/hooks/skill-usage-log.sh");
		const homeLogger = path.join(tmp, "home/.claude/hooks/skill-usage-log.sh");

		ok(
			"resolveLoggerScript: explicit override wins",
			resolveLoggerScript({ CCC_SKILL_USAGE_LOGGER: logger }) === logger,
		);
		ok(
			"resolveLoggerScript: missing override fails closed",
			resolveLoggerScript({ CCC_SKILL_USAGE_LOGGER: `${tmp}/absent.sh` }) === null,
		);
		ok(
			"resolveLoggerScript: CCC_CLAUDE_DIR beats HOME",
			resolveLoggerScript({
				CCC_CLAUDE_DIR: `${tmp}/claude`,
				HOME: `${tmp}/home`,
			}) === logger,
		);
		ok(
			"resolveLoggerScript: HOME default",
			resolveLoggerScript({ HOME: `${tmp}/home` }) === homeLogger,
		);
		ok("resolveLoggerScript: nowhere to look", resolveLoggerScript({}) === null);
		fs.symlinkSync(logger, `${tmp}/logger-link.sh`);
		ok("logger symlink rejected", resolveLoggerScript({ CCC_SKILL_USAGE_LOGGER: `${tmp}/logger-link.sh` }) === null);
	} finally {
		fs.rmSync(tmp, { recursive: true, force: true });
	}
}

// command existence — unknown or non-skill commands are not proven use
function groupCommandExists(): void {
	const { pi } = makePi(SKILL_COMMANDS);
	ok("existing skill command resolves", skillCommandExists(pi as never, "web"));
	ok("absent skill rejected", !skillCommandExists(pi as never, "no-such-skill"));
	ok("prompt command is not a skill", !skillCommandExists(pi as never, "doctor"));
	const broken = makePi(SKILL_COMMANDS);
	(broken.pi as { getCommands: () => never }).getCommands = () => {
		throw new Error("unsupported runtime");
	};
	ok("unsupported API fails open", !skillCommandExists(broken.pi as never, "web"));
}

// ---------------------------------------------------------------------------
// semantics — synthetic events against a capturing stub logger
// ---------------------------------------------------------------------------
async function groupSemantics(cap: string): Promise<void> {
	const { pi, handlers, usage } = makePi(SKILL_COMMANDS);
	skillUsageExtension(pi as never);

	ok("registers tool_call once", (handlers.get("tool_call") ?? []).length === 1);
	ok("registers tool_execution_end once", (handlers.get("tool_execution_end") ?? []).length === 1);
	ok("registers input once", (handlers.get("input") ?? []).length === 1);
	ok("registers session_shutdown once", (handlers.get("session_shutdown") ?? []).length === 1);

	const count = () => linesOf(cap).length;
	const skillPath = "/home/x/.claude/skills/gh-pr-flow/SKILL.md";

	// A successful SKILL.md read emits exactly one Read line.
	await fire(handlers, "tool_call", readCall("call-1", skillPath));
	await fire(handlers, "tool_execution_end", readEnd("call-1", false));
	const afterRead = await waitFor(count, (n) => n >= 1);
	ok("successful SKILL.md read captured once", afterRead === 1, `lines=${afterRead}`);
	const first = JSON.parse(linesOf(cap)[0]) as Record<string, unknown>;
	ok("read line carries Read tool_name", first.tool_name === "Read");
	ok(
		"read line carries the file_path",
		(first.tool_input as Record<string, unknown>).file_path === "/skills/gh-pr-flow/SKILL.md",
	);
	ok("pi.exec never used (it cannot carry stdin)", usage.exec === 0);
	await sleep(250);
	ok("no duplicate emission after settle", count() === 1, `lines=${count()}`);

	// A failed read is not use.
	await fire(handlers, "tool_call", readCall("call-2", "/home/x/.claude/skills/web/SKILL.md"));
	await fire(handlers, "tool_execution_end", readEnd("call-2", true));
	await sleep(300);
	ok("failed read not captured", count() === 1, `lines=${count()}`);

	// Missing/malformed success flags cannot prove a successful read.
	for (const flag of [undefined, null, 0, "false"]) {
		await fire(handlers, "tool_call", readCall("unproven", skillPath));
		await fire(handlers, "tool_execution_end", { ...readEnd("unproven", false), isError: flag });
	}
	await sleep(150);
	ok("missing or malformed success never counted", count() === 1);
	await fire(handlers, "tool_execution_end", readEnd("call-1", false));
	await sleep(150);
	ok("replayed completed tool call never counted twice", count() === 1);

	// Reads of non-skill paths are not use.
	await fire(handlers, "tool_call", readCall("call-3", "/home/x/notes/todo.md"));
	await fire(handlers, "tool_execution_end", readEnd("call-3", false));
	await sleep(300);
	ok("non-skill read not captured", count() === 1, `lines=${count()}`);

	// Piri does not strip URL-style fragments from file paths. A successful
	// read of this literal other filename is not a canonical SKILL.md read.
	await fire(handlers, "tool_call", readCall("call-4", "/home/x/.claude/skills/web/SKILL.md#other-file"));
	await fire(handlers, "tool_execution_end", readEnd("call-4", false));
	await sleep(200);
	ok("successful literal fragment filename is not skill evidence", count() === 1, `lines=${count()}`);
	await fire(handlers, "tool_call", readCall("call-4-plain", "/home/x/.claude/skills/web/SKILL.md"));
	await fire(handlers, "tool_execution_end", readEnd("call-4-plain", false));
	await waitFor(count, (n) => n >= 2);
	ok("exact SKILL.md still counts after excluded other filename", count() === 2, `lines=${count()}`);

	// Unicode and spaces survive the JSON round-trip.
	const unicodePath = "/home/우주 노드/.claude/skills/gh-pr-flow/SKILL.md";
	await fire(handlers, "tool_call", readCall("call-5", unicodePath));
	await fire(handlers, "tool_execution_end", readEnd("call-5", false));
	await waitFor(count, (n) => n >= 3);
	const third = JSON.parse(linesOf(cap)[2]) as Record<string, unknown>;
	ok(
		"unicode/spaced source path reduced to skill name",
		(third.tool_input as Record<string, unknown>).file_path === "/skills/gh-pr-flow/SKILL.md",
	);

	// Explicit /skill: requests are captured once, args stripped.
	await fire(handlers, "input", { type: "input", text: "/skill:web" });
	await waitFor(count, (n) => n >= 4);
	const fourth = JSON.parse(linesOf(cap)[3]) as Record<string, unknown>;
	ok("skill request carries Skill tool_name", fourth.tool_name === "Skill");
	ok(
		"skill request carries the name",
		(fourth.tool_input as Record<string, unknown>).skill === "web",
	);
	await fire(handlers, "input", { type: "input", text: "/skill:web fix the flake" });
	await waitFor(count, (n) => n >= 5);
	const fifth = JSON.parse(linesOf(cap)[4]) as Record<string, unknown>;
	ok(
		"skill request args stripped",
		(fifth.tool_input as Record<string, unknown>).skill === "web",
	);

	// Unknown, empty, and non-skill inputs are not use.
	const before = count();
	await fire(handlers, "input", { type: "input", text: "/skill:no-such-skill" });
	await fire(handlers, "input", { type: "input", text: "/skill:" });
	await fire(handlers, "input", { type: "input", text: "please look at the web skill docs" });
	await fire(handlers, "input", { type: "input", text: "/doctor" });
	await sleep(300);
	ok("invalid or merely-listed inputs not captured", count() === before, `lines=${count()}`);

	// An explicit request plus an actual read of the same skill are two
	// distinct truthful events — exactly two lines, not one, not four.
	await fire(handlers, "input", { type: "input", text: "/skill:gh-pr-flow" });
	await fire(handlers, "tool_call", readCall("call-6", skillPath));
	await fire(handlers, "tool_execution_end", readEnd("call-6", false));
	await waitFor(count, (n) => n >= before + 2);
	ok(
		"skill request + read are exactly two lines",
		count() === before + 2,
		`lines=${count()}`,
	);

	// Malformed events never throw and never capture.
	await fire(handlers, "tool_call", { type: "tool_call", toolName: "read" });
	await fire(handlers, "tool_call", { type: "tool_call", toolCallId: "c7", toolName: "read", input: {} });
	await fire(handlers, "tool_call", { type: "tool_call", toolCallId: "c8", toolName: "read", input: { path: 42 } });
	await fire(handlers, "tool_call", null);
	await fire(handlers, "tool_execution_end", { type: "tool_execution_end" });
	await fire(handlers, "tool_execution_end", null);
	await fire(handlers, "input", { type: "input" });
	await fire(handlers, "input", null);
	await sleep(300);
	ok("malformed events fail open", count() === before + 2, `lines=${count()}`);

	// session_shutdown drops pending reads (nothing captured afterwards).
	await fire(handlers, "tool_call", readCall("call-9", "/home/x/.claude/skills/web/SKILL.md"));
	await fire(handlers, "session_shutdown", {});
	await fire(handlers, "tool_execution_end", readEnd("call-9", false));
	await sleep(300);
	ok("pending read dropped at session shutdown", count() === before + 2, `lines=${count()}`);
	// Interleaved tool IDs must never borrow another read's path or success.
	await fire(handlers, "tool_call", readCall("mux-web", "/skills/web/SKILL.md"));
	await fire(handlers, "tool_call", readCall("mux-flow", skillPath));
	await fire(handlers, "tool_execution_end", readEnd("mux-flow", false));
	await fire(handlers, "tool_execution_end", readEnd("mux-web", true));
	await waitFor(count, (n) => n >= before + 3);
	const last = JSON.parse(linesOf(cap).at(-1) ?? "{}");
	ok("interleaved IDs retain path and success association", count() === before + 3 && last.tool_input?.file_path === "/skills/gh-pr-flow/SKILL.md");
}

// ---------------------------------------------------------------------------
// missing-logger — fail-open with no ledger write and no crash
// ---------------------------------------------------------------------------
async function groupMissingLogger(cap: string): Promise<void> {
	const { pi, handlers, usage } = makePi(SKILL_COMMANDS);
	skillUsageExtension(pi as never);
	await fire(handlers, "tool_call", readCall("m1", "/home/x/.claude/skills/web/SKILL.md"));
	await fire(handlers, "tool_execution_end", readEnd("m1", false));
	await fire(handlers, "input", { type: "input", text: "/skill:web" });
	await sleep(400);
	ok("no logger: nothing captured", linesOf(cap).length === 0);
	ok("no logger: pi.exec not used", usage.exec === 0);
	const result = await emitSkillUse("/nonexistent/logger.sh", payloadForSkill("web"), {
		timeoutMs: 1000,
	});
	ok("no logger: emit resolves undelivered", result.delivered === false);
}

// ---------------------------------------------------------------------------
// timeout — a hung logger is killed on the bound, never hangs the session
// ---------------------------------------------------------------------------
async function groupTimeout(): Promise<void> {
	const hungLogger = process.env.CCC_TEST_HUNG_LOGGER ?? "";
	const start = Date.now();
	const result = await emitSkillUse(hungLogger, payloadForRead("/x/skills/web/SKILL.md"), {
		timeoutMs: 200,
	});
	const elapsedMs = Date.now() - start;
	ok("hung logger: timed out", result.timedOut === true);
	ok("hung logger: not delivered", result.delivered === false);
	ok("hung logger: bounded quickly", elapsedMs < 5000, `elapsed=${elapsedMs}ms`);
}

// Descendants holding flock and excess concurrent emits must also be bounded.
async function groupResourceBounds(): Promise<void> {
	const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "skill-usage-bounds-"));
	try {
		const logger = path.join(tmp, "logger.sh");
		const lock = path.join(tmp, "group.lock");
		const pidFile = path.join(tmp, "child.pid");
		fs.writeFileSync(logger, 'exec 9>"$CCC_TEST_GROUP_LOCK"\nflock -x 9\nsleep 30 &\nprintf "%s\\n" "$!" >"$CCC_TEST_CHILD_PID"\nwait\n');
		const result = await emitSkillUse(logger, payloadForSkill("web"), {
			timeoutMs: 350,
			env: { ...process.env, CCC_TEST_GROUP_LOCK: lock, CCC_TEST_CHILD_PID: pidFile },
		});
		ok("descendant fixture reached locked background child", fs.existsSync(pidFile));
		ok("logger holding flock timed out", result.timedOut);
		await sleep(100);
		ok("timeout releases descendant-inherited flock", spawnSync("flock", ["-n", lock, "true"]).status === 0);
		const pid = fs.readFileSync(pidFile, "utf8").trim();
		const stat = `/proc/${pid}/stat`;
		ok("timed out descendant no longer executes", !fs.existsSync(stat) || fs.readFileSync(stat, "utf8").split(") ")[1]?.startsWith("Z ") === true);

		fs.writeFileSync(logger, 'exec 9>"$CCC_TEST_GROUP_LOCK"\nflock -x 9\nsleep 30 &\nprintf "%s\\n" "$!" >"$CCC_TEST_CHILD_PID"\nexit 0\n');
		const early = await emitSkillUse(logger, payloadForSkill("web"), {
			timeoutMs: 350,
			env: { ...process.env, CCC_TEST_GROUP_LOCK: lock, CCC_TEST_CHILD_PID: pidFile },
		});
		await sleep(100);
		ok("successful parent exit also cleans background lock holder", early.delivered && spawnSync("flock", ["-n", lock, "true"]).status === 0);

		const starts = path.join(tmp, "starts");
		fs.writeFileSync(logger, 'printf "started\\n" >>"$CCC_TEST_STARTS"\nsleep 30\n');
		const options = { timeoutMs: 350, env: { ...process.env, CCC_TEST_STARTS: starts } };
		const started = Date.now();
		const promises = Array.from({ length: 24 }, () => emitSkillUse(logger, payloadForSkill("web"), options));
		ok("foreground does not await logger completion", Date.now() - started < 250);
		const results = await Promise.all(promises);
		ok("no more than four concurrent logger children spawn", linesOf(starts).length === 4);
		ok("excess telemetry drops without unbounded queue", results.filter((r) => r.timedOut).length === 4);
		await sleep(100);
		fs.writeFileSync(logger, 'printf "started\\n" >>"$CCC_TEST_STARTS"\n');
		ok("child slots released after timeout", (await emitSkillUse(logger, payloadForSkill("web"), options)).delivered);
		const before = linesOf(starts).length;
		await emitSkillUse(logger, "x".repeat(8192), options);
		await sleep(100);
		ok("oversized payload rejected before spawning", linesOf(starts).length === before);
	} finally {
		fs.rmSync(tmp, { recursive: true, force: true });
	}
}

// ---------------------------------------------------------------------------
// integration — the REAL repo logger in a fixture HOME
// ---------------------------------------------------------------------------
async function groupIntegration(cap: string): Promise<void> {
	const { pi, handlers } = makePi(SKILL_COMMANDS);
	skillUsageExtension(pi as never);
	const home = process.env.HOME ?? "";
	const ledger = path.join(home, ".claude/state/skill-usage/usage.jsonl");
	const stateDir = path.join(home, ".claude/state/skill-usage");
	const ledgerLines = () => linesOf(ledger).length;

	await fire(handlers, "tool_call", readCall("i1", "/home/other/.claude/skills/web/SKILL.md"));
	await fire(handlers, "tool_execution_end", readEnd("i1", false));
	await waitFor(ledgerLines, (n) => n >= 1);
	ok("real logger: read appended one line", ledgerLines() === 1, `lines=${ledgerLines()}`);

	await fire(handlers, "input", { type: "input", text: "/skill:web" });
	await waitFor(ledgerLines, (n) => n >= 2);
	await sleep(250);
	ok("real logger: skill request appended one more", ledgerLines() === 2, `lines=${ledgerLines()}`);

	const first = JSON.parse(linesOf(ledger)[0]) as Record<string, unknown>;
	ok("real logger: skill name", first.skill === "web");
	ok("real logger: tool recorded", first.tool === "Read");
	ok("real logger: ts recorded", typeof first.ts === "string" && (first.ts as string).length > 0);
	const firstRaw = linesOf(ledger)[0];
	ok(
		"real logger: ledger line has no raw path (privacy)",
		!firstRaw.includes("SKILL.md") && !firstRaw.includes("file_path"),
	);
	const second = JSON.parse(linesOf(ledger)[1]) as Record<string, unknown>;
	ok("real logger: skill request tool", second.tool === "Skill");
	ok("real logger: ledger is owner-only", (fs.statSync(ledger).mode & 0o777) === 0o600);
	ok("real logger: state dir is owner-only", (fs.statSync(stateDir).mode & 0o777) === 0o700);
	ok(
		"real logger: no ledger under the piri agent dir",
		!fs.existsSync(path.join(home, ".piri", "state", "skill-usage")),
	);
	ok("integration: capture file empty (logger owns the ledger)", linesOf(cap).length === 0);
}

async function main(): Promise<void> {
	const group = process.argv[2] ?? "";
	const cap = process.env.CCC_TEST_CAPTURE_FILE ?? "";
	switch (group) {
		case "unit":
			await groupUnit();
			groupCommandExists();
			break;
		case "semantics":
			await groupSemantics(cap);
			break;
		case "missing-logger":
			await groupMissingLogger(cap);
			break;
		case "timeout":
			await groupTimeout();
			break;
		case "resource-bounds":
			await groupResourceBounds();
			break;
		case "integration":
			await groupIntegration(cap);
			break;
		default:
			console.log(`FAIL unknown group: ${group}`);
			process.exit(2);
	}
	console.log(`----`);
	console.log(`${group}: PASS=${passed} FAIL=${failed}`);
	if (failed > 0) process.exit(1);
}

await main();
