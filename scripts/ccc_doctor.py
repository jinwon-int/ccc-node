#!/usr/bin/env python3
"""ccc doctor — harness consistency diagnostics and conservative repair.

``--json`` stdout contract: stdout carries exactly one JSON document (optional
surrounding whitespace only), so a strict ``json.load`` consumer never sees
trailing data. Probe/subprocess diagnostics are routed to stderr while the report
is assembled. Exit code is independent of ``--json``: it is ``1`` when any
``교정가능`` (correctable) or ``수동필요`` (manual) finding is present and ``0``
otherwise; ``경고`` (warning) findings do not change the exit code.
"""

from __future__ import annotations

import contextlib
import filecmp
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HOOK_FILES = [
    "hooks/load-memory.sh",
    "hooks/load-tools.sh",
    "hooks/checkpoint.sh",
    "hooks/statusline.sh",
    "hooks/guard.sh",
    "hooks/audit.sh",
    "hooks/redact.sh",
    "hooks/notify.sh",
    "hooks/evidence-gate.sh",
]
OUTPUT_STYLE_FILES = ["output-styles/ccc-report.md"]
VALID_SCOPES = {"settings", "files", "hooks", "output-styles", "all"}
CODEX_PROBE_TIMEOUT_SECONDS = 5.0
CODEX_PROBE_TIMEOUT_MAX_SECONDS = 10.0


@dataclass
class Row:
    klass: str
    item: str
    status: str
    action: str


class Doctor:
    def __init__(self, repo: Path, claude_dir: Path, scope: str):
        self.repo = repo
        self.claude_dir = claude_dir
        self.settings = claude_dir / "settings.json"
        self.scope = scope
        self.rows: list[Row] = []
        self.counts = {"정상": 0, "경고": 0, "교정가능": 0, "수동필요": 0}
        self.mode = "unknown"
        self.provider = os.environ.get("CCC_AGENT_PROVIDER", "claude").strip().lower()
        self.readiness = "not-applicable"
        self.settings_valid = False
        self.current_settings: dict[str, Any] | None = None

    def add(self, klass: str, item: str, status: str, action: str) -> None:
        self.rows.append(Row(klass, item, status, action))
        self.counts[klass] += 1

    def scope_has(self, want: str) -> bool:
        parts = set(self.scope.split(","))
        if want == "settings":
            return "settings" in parts or "all" in parts
        if want == "hooks":
            return bool(parts & {"hooks", "files", "all"})
        if want == "output-styles":
            return bool(parts & {"output-styles", "files", "all"})
        return False

    def load_json(self, path: Path) -> Any:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)

    def json_ok(self, path: Path) -> bool:
        try:
            self.load_json(path)
            return True
        except Exception:
            return False

    def json_has_path(self, obj: Any, dotted: str) -> bool:
        cur = obj
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return False
            cur = cur[part]
        return True

    def harness_version(self) -> str:
        version = self.repo / "scripts" / "ccc-version.sh"
        if os.access(version, os.X_OK):
            try:
                return subprocess.check_output(
                    [str(version)], env={**os.environ, "CCC_VERSION_REPO_DIR": str(self.repo)}, text=True, stderr=subprocess.DEVNULL
                ).strip() or "unknown"
            except Exception:
                return "unknown"
        try:
            return subprocess.check_output(
                ["git", "-C", str(self.repo), "describe", "--tags", "--dirty", "--always"], text=True, stderr=subprocess.DEVNULL
            ).strip()
        except Exception:
            return "unknown"

    def diagnose(self) -> None:
        if not self.settings.exists():
            self.add("수동필요", "settings.json", "missing", "run setup.sh from the repo after backing up ~/.claude; install mode cannot be inferred safely")
        elif not self.json_ok(self.settings):
            self.add("수동필요", "settings.json", "invalid JSON", "repair JSON manually or restore from backup")
        else:
            self.settings_valid = True
            self.current_settings = self.load_json(self.settings)
            has_session = self.json_has_path(self.current_settings, "hooks.SessionStart")
            has_pretool = self.json_has_path(self.current_settings, "hooks.PreToolUse")
            if has_session and has_pretool:
                self.mode = "standalone"
            elif has_session and not has_pretool:
                self.mode = "plugin"
            elif not has_session and has_pretool:
                self.mode = "ambiguous"
            self.add("정상", "settings.json", f"valid JSON; mode: {self.mode}", "none")

        if self.settings_valid and self.current_settings is not None:
            if self.current_settings.get("outputStyle") == "ccc-report":
                self.add("정상", "outputStyle", "ccc-report", "none")
            else:
                self.add("교정가능", "outputStyle", "missing or not ccc-report", "restore settings from claude/settings.base.json")

            sl_cmd = str(self.current_settings.get("statusLine", {}).get("command", "") or "")
            if not sl_cmd:
                self.add("교정가능", "statusLine", "missing", "restore settings statusLine wiring")
            elif "statusline.sh" in sl_cmd:
                self.add("정상", "statusLine", sl_cmd, "none")
            else:
                self.add("교정가능", "statusLine", f"unexpected command: {sl_cmd}", "point statusLine at hooks/statusline.sh")

            for event in ("SessionStart", "PostCompact"):
                if self.json_has_path(self.current_settings, f"hooks.{event}"):
                    self.add("정상", f"hook wiring {event}", "present", "none")
                else:
                    self.add("교정가능", f"hook wiring {event}", "missing", "restore node-local hook wiring from settings.base.json")

            if self.mode == "standalone":
                for event in ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Notification", "Stop", "SessionEnd"):
                    if self.json_has_path(self.current_settings, f"hooks.{event}"):
                        self.add("정상", f"portable hook {event}", "settings-owned", "none")
                    else:
                        self.add("교정가능", f"portable hook {event}", "missing in standalone settings", "merge enforcement-overlay.json into settings.json")
            elif self.mode == "plugin":
                self.add("정상", "portable hooks", "plugin-owned mode detected", "do not merge enforcement-overlay into settings.json")
            else:
                self.add("수동필요", "install mode", "could not distinguish standalone vs plugin", "inspect settings.json/plugin ownership to avoid double-firing")

        for rel in HOOK_FILES:
            src = self.repo / "claude" / rel
            dst = self.claude_dir / rel
            if not dst.is_file():
                self.add("교정가능", rel, "missing", "run ccc-doctor --fix --apply --scope=files after backup to reinstall allowlisted harness files")
            elif src.is_file() and not filecmp.cmp(src, dst, shallow=False):
                self.add("교정가능", rel, "drifted", "run ccc-doctor --fix --apply --scope=files after backup to reinstall allowlisted harness files")
            else:
                self.add("정상", rel, "installed", "none")

        for rel in OUTPUT_STYLE_FILES:
            src = self.repo / "claude" / rel
            dst = self.claude_dir / rel
            if not dst.is_file():
                self.add("교정가능", rel, "missing", "run ccc-doctor --fix --apply --scope=files after backup to reinstall output styles")
            elif src.is_file() and not filecmp.cmp(src, dst, shallow=False):
                self.add("교정가능", rel, "drifted", "run ccc-doctor --fix --apply --scope=files after backup to reinstall output styles")
            else:
                self.add("정상", rel, "installed", "none")

        self.check_overlay_parity()
        self.check_bridge_status()
        self.check_memory_cache()
        self.check_provider_readiness()

    def codex_probe_timeout(self) -> float:
        value = os.environ.get("CCC_CODEX_READINESS_TIMEOUT", "")
        try:
            timeout = float(value) if value else CODEX_PROBE_TIMEOUT_SECONDS
        except ValueError:
            return CODEX_PROBE_TIMEOUT_SECONDS
        return min(max(timeout, 0.1), CODEX_PROBE_TIMEOUT_MAX_SECONDS)

    def resolve_codex_executable(self) -> tuple[Path | None, str | None]:
        configured = os.environ.get("CCC_CODEX_CLI_PATH", "codex").strip()
        if not configured:
            return None, "not found"
        if os.sep in configured or (os.altsep is not None and os.altsep in configured):
            candidate = Path(configured).expanduser()
            if not candidate.is_file():
                return None, "not found"
            if not os.access(candidate, os.X_OK):
                return None, "not executable"
            return candidate.resolve(), None
        resolved = shutil.which(configured)
        if resolved is None:
            return None, "not found"
        candidate = Path(resolved)
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            return None, "not executable"
        return candidate.resolve(), None

    def run_codex_probe(
        self, executable: Path, args: list[str]
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(executable), *args],
            text=True,
            capture_output=True,
            timeout=self.codex_probe_timeout(),
            check=False,
        )

    @staticmethod
    def probe_output(result: subprocess.CompletedProcess[str]) -> str:
        return (result.stdout + result.stderr)[:4096].strip()

    def fail_codex_readiness(self, item: str, status: str, action: str) -> None:
        self.readiness = "failed"
        self.add("수동필요", item, status, action)

    def check_provider_readiness(self) -> None:
        if self.provider == "claude":
            return
        if self.provider != "codex":
            self.readiness = "failed"
            self.add(
                "수동필요",
                "agent provider",
                "unsupported provider",
                "set CCC_AGENT_PROVIDER to claude or codex",
            )
            return

        executable, error = self.resolve_codex_executable()
        if executable is None:
            self.fail_codex_readiness(
                "Codex executable",
                error or "unavailable",
                "install Codex CLI or configure an executable CCC_CODEX_CLI_PATH",
            )
            return
        self.add("정상", "Codex executable", "executable", "none")

        probes = (
            (
                "Codex version probe",
                ["--version"],
                lambda output: bool(
                    re.search(r"\bcodex(?:-cli)?\b.*\d", output, re.IGNORECASE)
                ),
                "install a Codex CLI version with a working --version command",
            ),
            (
                "Codex app-server probe",
                ["app-server", "--help"],
                lambda output: bool(re.search(r"app[- ]server", output, re.IGNORECASE)),
                "install a Codex CLI version that exposes the app-server surface",
            ),
        )
        for item, args, valid, action in probes:
            try:
                result = self.run_codex_probe(executable, args)
            except subprocess.TimeoutExpired:
                self.fail_codex_readiness(item, "timed out", action)
                return
            except Exception:
                self.fail_codex_readiness(item, "probe failed", action)
                return
            output = self.probe_output(result)
            if result.returncode != 0:
                self.fail_codex_readiness(item, "probe failed", action)
                return
            if not output or not valid(output):
                self.fail_codex_readiness(item, "malformed output", action)
                return
            self.add("정상", item, "available", "none")

        try:
            login = self.run_codex_probe(executable, ["login", "status"])
        except subprocess.TimeoutExpired:
            self.fail_codex_readiness(
                "Codex login", "timed out", "authenticate Codex CLI, then rerun ccc-doctor"
            )
            return
        except Exception:
            self.fail_codex_readiness(
                "Codex login", "probe failed", "authenticate Codex CLI, then rerun ccc-doctor"
            )
            return
        login_output = self.probe_output(login)
        negative = re.search(
            r"\b(not logged in|not authenticated|unauthenticated|logged out)\b",
            login_output,
            re.IGNORECASE,
        )
        authenticated = re.search(
            r"\b(logged in|authenticated)\b", login_output, re.IGNORECASE
        )
        if login.returncode != 0 or negative:
            self.fail_codex_readiness(
                "Codex login",
                "not authenticated",
                "authenticate Codex CLI, then rerun ccc-doctor",
            )
            return
        if not login_output or not authenticated:
            self.fail_codex_readiness(
                "Codex login",
                "malformed output",
                "verify Codex CLI login status manually, then rerun ccc-doctor",
            )
            return
        self.add("정상", "Codex login", "authenticated", "none")
        self.readiness = "ready"

    def normalize_hook_manifest(self, path: Path) -> list[dict[str, Any]]:
        data = self.load_json(path)
        out = []
        for event, items in (data.get("hooks") or {}).items():
            norm_items = []
            for item in items or []:
                cmds = []
                for hook in item.get("hooks") or []:
                    cmd = str(hook.get("command", ""))
                    base = Path(cmd).name if "/" in cmd else cmd
                    cmds.append(base)
                norm_items.append({"m": item.get("matcher", ""), "c": sorted(cmds)})
            out.append({"event": event, "items": sorted(norm_items, key=lambda x: (x["m"], ",".join(x["c"])))})
        return sorted(out, key=lambda x: x["event"])

    def check_overlay_parity(self) -> None:
        overlay = self.repo / "claude/hooks/enforcement-overlay.json"
        hooks = self.repo / "claude/hooks/hooks.json"
        if overlay.is_file() and hooks.is_file():
            try:
                if self.normalize_hook_manifest(overlay) == self.normalize_hook_manifest(hooks):
                    self.add("정상", "overlay/plugin parity", "equivalent", "none")
                else:
                    self.add("교정가능", "overlay/plugin parity", "diverged", "sync enforcement-overlay.json and hooks/hooks.json before release")
            except Exception:
                self.add("경고", "overlay/plugin parity", "repo hook manifests unavailable", "run from a complete ccc-node checkout")
        else:
            self.add("경고", "overlay/plugin parity", "repo hook manifests unavailable", "run from a complete ccc-node checkout")

    def check_bridge_status(self) -> None:
        start = self.repo / "bridge/start.sh"
        if os.access(start, os.X_OK):
            try:
                out = subprocess.run([str(start), "--path", "/root", "--status"], text=True, capture_output=True, timeout=20)
                tail = "\n".join((out.stdout + out.stderr).splitlines()[-5:])
                if tail:
                    self.add("정상", "bridge status", "readable", "none")
                else:
                    self.add("경고", "bridge status", "no status output", "check bridge/start.sh manually if this node owns Telegram bridge")
            except Exception:
                self.add("경고", "bridge status", "no status output", "check bridge/start.sh manually if this node owns Telegram bridge")
        else:
            self.add("경고", "bridge status", "bridge/start.sh missing or not executable", "not all nodes run the Telegram bridge; install/check only if needed")

    def check_memory_cache(self) -> None:
        script = self.repo / "scripts/ccc-memory-check.sh"
        if os.access(script, os.X_OK):
            env = os.environ.copy()
            env.setdefault("CCC_STATE_DIR", str(self.claude_dir / "state"))
            env.setdefault("CCC_MEMORY_CACHE_DIR", str(self.claude_dir / "hooks/cache"))
            try:
                out = subprocess.run([str(script), "--json"], text=True, capture_output=True, env=env, timeout=20).stdout.strip()
                mem = json.loads(out) if out else None
            except Exception:
                mem = None
            if isinstance(mem, dict):
                wiki = (mem.get("wiki") or {}).get("status", "unknown")
                honcho = (mem.get("honcho") or {}).get("status", "unknown")
                idx = (mem.get("local_index") or {}).get("exists", False)
                status = f"wiki={wiki}; honcho={honcho}; local_index={str(idx).lower()}"
                if wiki == "ok" and honcho in {"ok", "disabled"}:
                    self.add("정상", "memory cache", status, "none")
                else:
                    self.add("경고", "memory cache", status, "run scripts/ccc-memory-check.sh --json and inspect stale/missing cache metadata")
            else:
                self.add("경고", "memory cache", "diagnostic unavailable", "run scripts/ccc-memory-check.sh manually")
        else:
            self.add("경고", "memory cache", "ccc-memory-check.sh missing", "complete checkout or reinstall scripts")

    def print_report(self) -> None:
        print("# ccc doctor\n")
        print(f"- repo: `{self.repo}`")
        print(f"- harness version: `{self.harness_version()}`")
        print(f"- claude dir: `{self.claude_dir}`")
        print(f"- mode: `{self.mode}`")
        print(f"- provider: `{self.provider}`")
        print(f"- readiness: `{self.readiness}`\n")
        print("## 진단 요약\n")
        print(f"- 정상: {self.counts['정상']}")
        print(f"- 경고: {self.counts['경고']}")
        print(f"- 교정가능: {self.counts['교정가능']}")
        print(f"- 수동필요: {self.counts['수동필요']}\n")
        print("| 분류 | 항목 | 상태 | 조치 |")
        print("|---|---|---|---|")
        for row in self.rows:
            print(f"| {row.klass} | `{row.item}` | {row.status} | {row.action} |")
        print("\n## 경계\n")
        print("- Diagnostics are read-only unless `--fix --apply` or `--rollback --apply` is explicitly used.")
        print("- `--fix` and `--rollback` alone are dry-run only.")
        print("- No remote nodes, secrets, broker/Gateway restarts, bridge restarts, migrations, or provider sends are touched.")

    def json_report_text(self) -> str:
        report = {
            "repo": str(self.repo),
            "harnessVersion": self.harness_version(),
            "claudeDir": str(self.claude_dir),
            "mode": self.mode,
            "provider": self.provider,
            "readiness": self.readiness,
            "counts": self.counts,
            "rows": [
                {
                    "class": row.klass,
                    "item": row.item,
                    "status": row.status,
                    "action": row.action,
                }
                for row in self.rows
            ],
        }
        return json.dumps(report, ensure_ascii=False, sort_keys=True)

    def print_json_report(self) -> None:
        print(self.json_report_text())

    def report_exit_code(self) -> int:
        """Exit non-zero for correctable/manual findings; warnings do not count."""

        return 1 if self.counts["수동필요"] > 0 or self.counts["교정가능"] > 0 else 0

    def desired_settings(self) -> dict[str, Any] | None:
        if not self.settings_valid or self.mode not in {"standalone", "plugin"} or self.current_settings is None:
            return None
        base_path = self.repo / "claude/settings.base.json"
        if not base_path.is_file():
            return None
        desired = json.loads(json.dumps(self.current_settings))
        base = self.load_json(base_path)
        desired["outputStyle"] = base.get("outputStyle")
        desired["statusLine"] = base.get("statusLine")
        desired.setdefault("hooks", {})
        for event in ("SessionStart", "PostCompact"):
            desired["hooks"][event] = (base.get("hooks") or {}).get(event)
        if self.mode == "standalone":
            overlay_path = self.repo / "claude/hooks/enforcement-overlay.json"
            if not overlay_path.is_file():
                return None
            overlay = self.load_json(overlay_path)
            for event in ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Notification", "Stop", "SessionEnd"):
                desired["hooks"][event] = (overlay.get("hooks") or {}).get(event)
        return desired

    def settings_needs_repair(self) -> bool:
        desired = self.desired_settings()
        if desired is None or self.current_settings is None:
            return False
        return json.dumps(self.current_settings, sort_keys=True, separators=(",", ":")) != json.dumps(desired, sort_keys=True, separators=(",", ":"))

    def validate_settings_backup(self, archive: Path | None) -> bool:
        if not archive or not archive.is_file():
            return False
        return subprocess.run(["tar", "-tzf", str(archive), "settings.json"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0

    def timestamp(self) -> str:
        return subprocess.check_output(["date", "+%Y%m%d-%H%M%S"], text=True).strip()

    def apply_settings_repair(self) -> bool:
        desired = self.desired_settings()
        if desired is None or not self.settings_needs_repair():
            return False
        ts = self.timestamp()
        backup_dir = self.claude_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        archive = backup_dir / f"ccc-doctor-{ts}.tar.gz"
        ok = subprocess.run(["tar", "-czf", str(archive), "-C", str(self.claude_dir), "settings.json"]).returncode == 0
        if not ok or not self.validate_settings_backup(archive):
            print(f"failed to create valid settings backup: {archive}", file=sys.stderr)
            return False
        self.settings.write_text(json.dumps(desired, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"applied settings.json repair; backup={archive}")
        return True

    def latest_rollback_backup(self) -> Path | None:
        backup_dir = self.claude_dir / "backups"
        if not backup_dir.is_dir():
            return None
        backups = list(backup_dir.glob("ccc-doctor-[0-9]*.tar.gz"))
        return max(backups, key=lambda p: p.stat().st_mtime) if backups else None

    def apply_settings_rollback(self, archive: Path) -> bool:
        if not self.validate_settings_backup(archive):
            return False
        ts = self.timestamp()
        backup_dir = self.claude_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        pre_archive = backup_dir / f"ccc-doctor-pre-rollback-{ts}.tar.gz"
        if self.settings.is_file():
            subprocess.run(["tar", "-czf", str(pre_archive), "-C", str(self.claude_dir), "settings.json"], check=False)
        subprocess.run(["tar", "-xzf", str(archive), "-C", str(self.claude_dir), "settings.json"], check=True)
        print(f"applied settings.json rollback; restored={archive}; preRollbackBackup={pre_archive}")
        return True

    def file_repair_list(self) -> list[str]:
        out: list[str] = []
        groups: list[str] = []
        if self.scope_has("hooks"):
            groups += HOOK_FILES
        if self.scope_has("output-styles"):
            groups += OUTPUT_STYLE_FILES
        for rel in groups:
            src = self.repo / "claude" / rel
            dst = self.claude_dir / rel
            if not dst.is_file() or (src.is_file() and not filecmp.cmp(src, dst, shallow=False)):
                out.append(rel)
        return out

    def is_path_under(self, path: Path, root: Path) -> bool:
        p = path.resolve(strict=False)
        r = root.resolve(strict=False)
        return p == r or r in p.parents

    def validate_file_repair_target(self, rel: str) -> bool:
        src = self.repo / "claude" / rel
        dst = self.claude_dir / rel
        if not (rel.startswith("hooks/") or rel.startswith("output-styles/")):
            print(f"unsupported repair target: {rel}", file=sys.stderr)
            return False
        if not src.is_file():
            print(f"source file missing: {src}", file=sys.stderr)
            return False
        if src.is_symlink():
            print(f"source symlink refused: {src}", file=sys.stderr)
            return False
        if dst.parent.is_symlink():
            print(f"destination parent symlink refused: {dst.parent}", file=sys.stderr)
            return False
        if dst.is_symlink():
            print(f"destination symlink refused: {dst}", file=sys.stderr)
            return False
        if rel.startswith("hooks/") and not self.is_path_under(dst, self.claude_dir / "hooks"):
            print(f"destination escapes hooks dir: {dst}", file=sys.stderr)
            return False
        if rel.startswith("output-styles/") and not self.is_path_under(dst, self.claude_dir / "output-styles"):
            print(f"destination escapes output-styles dir: {dst}", file=sys.stderr)
            return False
        return True

    def backup_file_repairs(self, rels: list[str]) -> Path:
        ts = self.timestamp()
        backup_dir = self.claude_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        archive = backup_dir / f"ccc-doctor-files-{ts}.tar.gz"
        existing = [rel for rel in rels if (self.claude_dir / rel).exists()]
        if existing:
            with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as fh:
                for rel in existing:
                    fh.write(rel + "\n")
                list_path = fh.name
            try:
                subprocess.run(["tar", "-czf", str(archive), "-C", str(self.claude_dir), "-T", list_path], check=False)
            finally:
                Path(list_path).unlink(missing_ok=True)
        else:
            subprocess.run(["tar", "-czf", str(archive), "-C", str(self.claude_dir), "--files-from", "/dev/null", "--warning=no-file-changed"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            (backup_dir / f"ccc-doctor-files-{ts}.manifest.txt").write_text("no pre-existing files for scoped repair\n", encoding="utf-8")
        return archive

    def apply_file_repairs(self) -> bool:
        rels = self.file_repair_list()
        if not rels:
            return False
        if self.mode != "standalone":
            print(f"install mode is {self.mode}; refusing scoped file repair to avoid plugin/standalone double-firing.", file=sys.stderr)
            return False
        for rel in rels:
            if not self.validate_file_repair_target(rel):
                return False
        archive = self.backup_file_repairs(rels)
        for rel in rels:
            src = self.repo / "claude" / rel
            dst = self.claude_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            try:
                shutil.copymode(src, dst)
            except Exception:
                pass
        print(f"applied scoped file repair; backup={archive}; repaired={','.join(rels)}")
        return True


def parse_args(argv: list[str]) -> tuple[int, bool, bool, bool, bool, str]:
    fix = rollback = apply = json_output = False
    scope = "settings"
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--fix":
            fix = True
        elif arg == "--rollback":
            rollback = True
        elif arg in {"--apply", "--write"}:
            apply = True
        elif arg == "--json":
            json_output = True
        elif arg == "--scope":
            if i + 1 >= len(argv):
                print("--scope requires a value", file=sys.stderr)
                return 2, fix, rollback, apply, json_output, scope
            i += 1
            scope = argv[i]
        elif arg.startswith("--scope="):
            scope = arg.split("=", 1)[1]
        elif arg in {"-h", "--help"}:
            print("Usage: ccc-doctor.sh [--fix [--apply] [--scope=settings|files|hooks,output-styles]] [--rollback [--apply]]")
            print()
            print("Diagnostics classify checks as: 정상 / 경고 / 교정가능 / 수동필요.")
            print()
            print("Repair boundary:")
            print("- `--fix` is a dry-run plan and makes no filesystem changes.")
            print("- `--fix --apply` defaults to `--scope=settings` and writes only deterministic")
            print("  settings.json repairs for 교정가능 outputStyle/statusLine/hook wiring drift,")
            print("  after a backup tar is created.")
            print("- `--fix --apply --scope=files` reinstalls only allowlisted hook scripts and")
            print("  output-style files from the repo after a scoped backup. It refuses symlinks,")
            print("  path traversal, missing repo sources, and ambiguous/manual install modes.")
            print("- `--rollback` is a dry-run plan that selects the latest ccc-doctor settings backup.")
            print("- `--rollback --apply` restores only settings.json from that backup, after backing up")
            print("  the current settings.json as `ccc-doctor-pre-rollback-*.tar.gz`.")
            print("- 수동필요/risky/system-level items fail closed and are never auto-repaired.")
            print("- `--json` writes exactly one JSON object to stdout (surrounding whitespace")
            print("  only); probe/subprocess diagnostics go to stderr so stdout stays strictly")
            print("  machine-parseable. `--fix`/`--rollback` take precedence and emit human text.")
            print()
            print("Exit codes (human and --json alike): 0 when only 정상/경고 findings exist; 1")
            print("when any 교정가능 (correctable) or 수동필요 (manual) finding is present. 경고")
            print("(warning) findings do not change the exit code.")
            return 0, fix, rollback, apply, json_output, scope
        else:
            print(f"Unknown flag: {arg}", file=sys.stderr)
            return 2, fix, rollback, apply, json_output, scope
        i += 1
    parts = scope.split(",") if scope else [""]
    if any(part not in VALID_SCOPES for part in parts):
        print(f"unsupported --scope: {scope}", file=sys.stderr)
        return 2, fix, rollback, apply, json_output, scope
    return -1, fix, rollback, apply, json_output, scope


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte to ``fd``, looping over partial ``os.write`` results.

    ``os.write`` is permitted to consume fewer bytes than requested — notably on
    pipes, where the field failure was observed — so a single call can truncate
    the sole JSON document and break the strict ``json.load(stdout)`` contract.
    Loop until the whole buffer is written and fail loudly on a zero-progress
    write rather than emit a partial document.
    """

    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while emitting the JSON report")
        view = view[written:]


def emit_json_report(doctor: Doctor) -> int:
    """Diagnose and write exactly one JSON document to stdout.

    stdout must stay strictly machine-parseable — a single JSON object with only
    optional surrounding whitespace — so a strict ``json.load`` consumer never
    fails on trailing bytes. A probe (or a helper it spawns, e.g. a Codex
    subprocess that reopens the inherited stdout descriptor) could otherwise
    trail non-JSON bytes after the report. To make the contract structural rather
    than best-effort, the real stdout file descriptor is redirected to stderr for
    the entire diagnosis and the JSON document is written to a preserved private
    copy of the original stdout, which is then closed. Any stray write — a stray
    ``print`` or a descriptor-inheriting subprocess — therefore lands on stderr,
    and nothing in this process can reach real stdout after the JSON document.
    """

    sys.stdout.flush()
    sys.stderr.flush()
    real_stdout_fd = os.dup(1)
    try:
        # Point fd 1 at stderr for the whole diagnosis so descriptor-inheriting
        # writers cannot reach the JSON stream; redirect_stdout catches Python
        # level prints too.
        os.dup2(2, 1)
        with contextlib.redirect_stdout(sys.stderr):
            doctor.diagnose()
            payload = doctor.json_report_text()
        _write_all(real_stdout_fd, (payload + "\n").encode("utf-8"))
    finally:
        os.close(real_stdout_fd)
    return doctor.report_exit_code()


def main(argv: list[str]) -> int:
    parsed_rc, fix, rollback, apply, json_output, scope = parse_args(argv)
    if parsed_rc >= 0:
        return parsed_rc
    repo = Path(os.environ.get("CCC_DOCTOR_REPO_DIR", Path(__file__).resolve().parents[1])).resolve()
    claude_dir = Path(os.environ.get("CCC_DOCTOR_CLAUDE_DIR", str(Path.home() / ".claude"))).resolve()
    doctor = Doctor(repo, claude_dir, scope)

    # Pure --json report: diagnose under a stdout guard so stdout stays a single
    # JSON document. --fix/--rollback intentionally emit human-readable stdout and
    # take precedence over --json, matching the prior behavior.
    if json_output and not fix and not rollback:
        return emit_json_report(doctor)

    doctor.diagnose()

    if rollback:
        print("# ccc doctor --rollback\n")
        archive = doctor.latest_rollback_backup()
        if not doctor.validate_settings_backup(archive):
            print("no rollback backup found; refusing automatic rollback.", file=sys.stderr)
            return 1
        assert archive is not None
        if apply:
            if not doctor.apply_settings_rollback(archive):
                print("rollback backup is invalid; refusing automatic rollback.", file=sys.stderr)
                return 1
            return 0
        print(f"dry-run: would restore settings.json from {archive}. Re-run with `--rollback --apply` to write after pre-rollback backup.")
        return 1

    if fix:
        print("# ccc doctor --fix\n")
        if doctor.counts["수동필요"] > 0:
            print("manual items present; refusing automatic repair.", file=sys.stderr)
            doctor.print_report()
            return 1
        settings_needed = doctor.scope_has("settings") and doctor.settings_needs_repair()
        rels = doctor.file_repair_list() if (doctor.scope_has("hooks") or doctor.scope_has("output-styles")) else []
        files_needed = bool(rels)
        if settings_needed or files_needed:
            if apply:
                if settings_needed and not doctor.apply_settings_repair():
                    return 1
                if files_needed and not doctor.apply_file_repairs():
                    return 1
                return 0
            if settings_needed:
                print("dry-run: would repair settings.json from canonical repo templates. Re-run with `--fix --apply` to write after backup.")
            if files_needed:
                print(f"dry-run: would reinstall scoped files from canonical repo templates: {','.join(rels)}. Re-run with `--fix --apply --scope={scope}` to write after backup.")
            return 1
        if (doctor.scope_has("hooks") or doctor.scope_has("output-styles")) and not apply:
            print("no scoped file repairs needed.")
        else:
            print("no repairs needed.")
        return 0

    doctor.print_report()
    return doctor.report_exit_code()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
