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
import importlib.util
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Fallback only. The live list is walked from the repo (see hook_files) using the
# same rule as ccc_hook_tree_files in scripts/lib/harness-paths.sh, which setup.sh
# and validate-harness.sh already share. This list used to be authoritative and
# silently omitted distill.sh, refresh-memory.sh, scan-injection.sh and
# skill-review.sh, so doctor reported a clean node while distill.sh sat 42 lines
# behind and was missing the #386 fleet autonomy kill-switch entirely.
HOOK_FILES_FALLBACK = [
    "hooks/load-memory.sh",
    "hooks/load-tools.sh",
    "hooks/checkpoint.sh",
    "hooks/statusline.sh",
    "hooks/audit.sh",
    "hooks/redact.sh",
    "hooks/notify.sh",
    "hooks/evidence-gate.sh",
]
# Mirrors the exclusions in ccc_hook_tree_files (harness-paths.sh).
HOOK_TREE_SKIP_NAMES = {"test-stub.sh", "hooks.json", "enforcement-overlay.json"}
HOOK_TREE_SKIP_SUFFIXES = (".test.sh", ".pyc", ".md")
OUTPUT_STYLE_FILES = ["output-styles/ccc-report.md"]
# setup.sh installs four trees; doctor used to watch two, so a stale skill,
# agent or slash command was invisible to both /doctor and self-update's
# check.sh (#1037). These three are detection-only: the --fix repair path
# refuses anything outside hooks/output-styles, and repairing them correctly
# needs setup.sh's per-skill staging and manifest prune.
#
# Each tree is installed differently, and the rules are mirrored here rather
# than guessed:
#   commands/  plain `cp claude/commands/*.md` — top level only.
#   agents/    top-level *.md; the a2a-* worker roster is role-gated, so it is
#              only expected when this node opted in (CCC_A2A_ROLE=worker or
#              the persisted ~/.claude/a2a-role marker).
#   skills/    per-skill directory trees from TWO repo roots. Skills whose name
#              is not in the repo set (node-local, autosave) are never touched
#              by setup.sh and must never be reported here.
SKILL_SOURCE_ROOTS = ("claude/skills", "skills/shared")
VALID_SCOPES = {"settings", "files", "hooks", "output-styles", "all"}
CODEX_PROBE_TIMEOUT_SECONDS = 5.0
CODEX_PROBE_TIMEOUT_MAX_SECONDS = 10.0

# Installer-managed cron markers (#1081). Maps the marker token each installer
# appends to its rendered line(s) to the repo script whose content hash is the
# expected `gen=` stamp (#1140 stamps with exactly that one file). Mirrors the
# render sites: install-memory-refresh-cron.sh, install-pr-status-poll-cron.sh,
# install-skill-autosave-cron.sh (CRON_LINE only), install-nunchi.sh
# (feed/refresh/bench). install-termux-mempalace.sh delegates cron rendering to
# install-nunchi.sh, so its lines are covered by the nunchi entry.
INSTALLER_CRON_MARKERS = {
    "# ccc-node:memory-refresh": "install-memory-refresh-cron.sh",
    "# ccc-node:pr-status-poll": "install-pr-status-poll-cron.sh",
    "# ccc-node:skill-autosave": "install-skill-autosave-cron.sh",
    "# nunchi:#816": "install-nunchi.sh",
}
# install-skill-autosave-cron.sh manages its schedule-comment block with these
# exact-match markers; they are deliberately unstamped (#1140) and are neither
# drift candidates nor ownerless.
CRON_BLOCK_MARKER_RE = re.compile(r"#\s*ccc-node:autosave-schedule:(?:begin|end)\b")
# Any other `# ccc-node:*` / `# nunchi:*` token has no installer in this repo
# (e.g. the hand-installed self-update / live-backups-rotate lines) and is
# surfaced as informational so residue like #1079's ghost entries is visible.
CRON_OWNERLESS_MARKER_RE = re.compile(r"#\s*(?:ccc-node|nunchi):\S+")
CRON_GEN_STAMP_RE = re.compile(r"\bgen=(h_[0-9a-f]{12})\b")

_CANONICAL_PATHS: Any = None
_CANONICAL_PATHS_TRIED = False


def canonical_paths_module() -> Any:
    """setup.sh's canonical-path rewrite, loaded from scripts/lib.

    Loaded by file path rather than import: scripts/lib is not a package, and
    mutating sys.path in a diagnostic tool risks shadowing later imports.
    Returns None when the checkout is incomplete, so callers fall back to a
    byte-exact comparison instead of guessing at the transform.
    """
    global _CANONICAL_PATHS, _CANONICAL_PATHS_TRIED
    if _CANONICAL_PATHS_TRIED:
        return _CANONICAL_PATHS
    _CANONICAL_PATHS_TRIED = True
    source = Path(__file__).resolve().parent / "lib" / "canonical_paths.py"
    try:
        spec = importlib.util.spec_from_file_location("_ccc_canonical_paths", source)
        if spec is not None and spec.loader is not None:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _CANONICAL_PATHS = module
    except Exception:
        _CANONICAL_PATHS = None
    return _CANONICAL_PATHS


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
        self.distill_readiness = "not-applicable"
        self.settings_valid = False
        self.current_settings: dict[str, Any] | None = None
        self._rewrite_pairs: dict[str, str] | None = None
        self._bridge_provider_state: tuple[str, str] | None = None

    def add(self, klass: str, item: str, status: str, action: str) -> None:
        self.rows.append(Row(klass, item, status, action))
        self.counts[klass] += 1

    def rewrite_pairs(self) -> dict[str, str]:
        """Canonical -> actual path substitutions setup.sh applied on this node."""
        if self._rewrite_pairs is None:
            module = canonical_paths_module()
            self._rewrite_pairs = (
                {} if module is None else module.rewrite_pairs(self.repo, self.claude_dir)
            )
        return self._rewrite_pairs

    def expected_installed_text(self, src: Path) -> str | None:
        """Template content as setup.sh would have installed it on this node.

        None means "no transform applies" — either the node uses the canonical
        paths, or the template is not decodable text — and the caller must fall
        back to a byte-exact comparison.
        """
        pairs = self.rewrite_pairs()
        if not pairs:
            return None
        module = canonical_paths_module()
        if module is None:
            return None
        try:
            return str(module.rewrite_text(src.read_text(encoding="utf-8"), pairs))
        except (OSError, UnicodeDecodeError):
            return None

    def installed_matches_source(self, src: Path, dst: Path) -> bool:
        """True when the installed file is what setup.sh would install here.

        Byte-exact for canonical installs; for every other install the template
        is compared through setup.sh's canonical-path rewrite, so a correctly
        installed /root/ccc-node or Termux node is not reported as drifted.
        """
        expected = self.expected_installed_text(src)
        if expected is None:
            return filecmp.cmp(src, dst, shallow=False)
        try:
            return dst.read_text(encoding="utf-8") == expected
        except (OSError, UnicodeDecodeError):
            return False

    def install_source_file(self, src: Path, dst: Path) -> None:
        """Install a template the same way setup.sh does, rewrite included."""
        expected = self.expected_installed_text(src)
        if expected is None:
            shutil.copyfile(src, dst)
        else:
            dst.write_text(expected, encoding="utf-8")
        try:
            shutil.copymode(src, dst)
        except Exception:
            pass

    def check_canonical_rewrite(self) -> None:
        if canonical_paths_module() is None:
            self.add(
                "수동필요", "canonical path rewrite", "shared transform unreadable",
                "restore scripts/lib/canonical_paths.py in this checkout, then rerun",
            )
            return
        pairs = self.rewrite_pairs()
        if not pairs:
            self.add("정상", "canonical path rewrite", "not needed (canonical install paths)", "none")
            return
        detail = "; ".join(f"{old} -> {new}" for old, new in pairs.items())
        self.add("정상", "canonical path rewrite", f"installed copies rewritten ({detail})", "none")

    def hook_files(self) -> list[str]:
        """Deployable hook tree, walked from the repo — never a hand-kept list.

        Same rule as ccc_hook_tree_files (scripts/lib/harness-paths.sh), which
        setup.sh deploys from: whatever setup installs is what doctor watches, so
        a new hook cannot land outside doctor's view. Falls back to the historical
        list when the repo tree is unreadable.
        """
        root = self.repo / "claude" / "hooks"
        if not root.is_dir():
            return list(HOOK_FILES_FALLBACK)
        out: list[str] = []
        for path in root.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            name = path.name
            if name in HOOK_TREE_SKIP_NAMES or name.endswith(HOOK_TREE_SKIP_SUFFIXES):
                continue
            out.append(f"hooks/{path.relative_to(root).as_posix()}")
        return sorted(out) or list(HOOK_FILES_FALLBACK)

    def a2a_role(self) -> str:
        """This node's opted-in A2A role, as setup.sh resolves it."""
        env = os.environ.get("CCC_A2A_ROLE", "").strip()
        if env:
            return env
        marker = self.claude_dir / "a2a-role"
        try:
            return marker.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return ""

    def managed_tree_files(self) -> list[tuple[Path, str]]:
        """(repo source, installed relative path) for commands/agents/skills.

        Mirrors setup.sh's install rules so that what setup deploys is what
        doctor watches. Returns pairs rather than relative strings because the
        skills tree is sourced from two repo roots, only one of which lives
        under claude/.
        """
        out: list[tuple[Path, str]] = []

        # Plain `cp` into the harness root. settings.json is deliberately absent:
        # setup composes it from base + overlay, so it has its own JSON-semantic
        # checks above rather than a file comparison, and settings.local.json is
        # node-local (seeded only when missing).
        headless = self.repo / "claude" / "headless.sh"
        if headless.is_file():
            out.append((headless, "headless.sh"))

        commands_root = self.repo / "claude" / "commands"
        if commands_root.is_dir():
            for path in sorted(commands_root.glob("*.md")):
                if path.is_file():
                    out.append((path, f"commands/{path.name}"))

        agents_root = self.repo / "claude" / "agents"
        if agents_root.is_dir():
            worker = self.a2a_role() == "worker"
            for path in sorted(agents_root.glob("*.md")):
                if not path.is_file():
                    continue
                # The roster is deliberately absent on broker/unconfigured
                # nodes; reporting it missing there would be a false alarm.
                if path.name.startswith("a2a-") and not worker:
                    continue
                out.append((path, f"agents/{path.name}"))

        for root_rel in SKILL_SOURCE_ROOTS:
            root = self.repo / root_rel
            if not root.is_dir():
                continue
            for skill_dir in sorted(p for p in root.iterdir() if p.is_dir()):
                for path in sorted(skill_dir.rglob("*")):
                    if not path.is_file() or "__pycache__" in path.parts:
                        continue
                    rel_in_skill = path.relative_to(skill_dir).as_posix()
                    out.append((path, f"skills/{skill_dir.name}/{rel_in_skill}"))

        return out

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
                # Invoked through an explicit bash, not as a bare executable:
                # the script's `#!/usr/bin/env bash` shebang cannot be resolved
                # on Termux (no /usr/bin/env), where direct exec raises ENOENT.
                out = subprocess.check_output(
                    ["bash", str(version)], env={**os.environ, "CCC_VERSION_REPO_DIR": str(self.repo)}, text=True, stderr=subprocess.DEVNULL
                ).strip()
                if out:
                    return out
            except Exception:
                pass  # fall through to git describe rather than giving up (#771)
        try:
            return subprocess.check_output(
                ["git", "-C", str(self.repo), "describe", "--tags", "--dirty", "--always"], text=True, stderr=subprocess.DEVNULL
            ).strip()
        except Exception:
            return "unknown"

    def diagnose(self) -> None:  # noqa: C901 -- #348 baseline hotspot
        if not self.settings.exists():
            self.add("수동필요", "settings.json", "missing", "run setup.sh from the repo after backing up ~/.claude; install mode cannot be inferred safely")
        elif not self.json_ok(self.settings):
            self.add("수동필요", "settings.json", "invalid JSON", "repair JSON manually or restore from backup")
        else:
            self.settings_valid = True
            self.current_settings = self.load_json(self.settings)
            has_session = self.json_has_path(self.current_settings, "hooks.SessionStart")
            # Mode detection keys off an overlay-owned event. PostToolUse
            # (audit) replaced PreToolUse here after TM-1306 removed the
            # semantic guard from the enforcement overlay.
            has_portable = self.json_has_path(self.current_settings, "hooks.PostToolUse")
            if has_session and has_portable:
                self.mode = "standalone"
            elif has_session and not has_portable:
                self.mode = "plugin"
            elif not has_session and has_portable:
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
                # Overlay events post-TM-1306 (no PreToolUse guard).
                for event in ("PostToolUse", "UserPromptSubmit", "Notification", "Stop", "SessionEnd"):
                    if self.json_has_path(self.current_settings, f"hooks.{event}"):
                        self.add("정상", f"portable hook {event}", "settings-owned", "none")
                    else:
                        self.add("교정가능", f"portable hook {event}", "missing in standalone settings", "merge enforcement-overlay.json into settings.json")
            elif self.mode == "plugin":
                self.add("정상", "portable hooks", "plugin-owned mode detected", "do not merge enforcement-overlay into settings.json")
            else:
                self.add("수동필요", "install mode", "could not distinguish standalone vs plugin", "inspect settings.json/plugin ownership to avoid double-firing")

        self.check_canonical_rewrite()

        for rel in self.hook_files():
            src = self.repo / "claude" / rel
            dst = self.claude_dir / rel
            if not dst.is_file():
                self.add("교정가능", rel, "missing", "run ccc-doctor --fix --apply --scope=files after backup to reinstall allowlisted harness files")
            elif src.is_file() and not self.installed_matches_source(src, dst):
                self.add("교정가능", rel, "drifted", "run ccc-doctor --fix --apply --scope=files after backup to reinstall allowlisted harness files")
            else:
                self.add("정상", rel, "installed", "none")

        for rel in OUTPUT_STYLE_FILES:
            src = self.repo / "claude" / rel
            dst = self.claude_dir / rel
            if not dst.is_file():
                self.add("교정가능", rel, "missing", "run ccc-doctor --fix --apply --scope=files after backup to reinstall output styles")
            elif src.is_file() and not self.installed_matches_source(src, dst):
                self.add("교정가능", rel, "drifted", "run ccc-doctor --fix --apply --scope=files after backup to reinstall output styles")
            else:
                self.add("정상", rel, "installed", "none")

        # Detection only: --fix --apply --scope=files refuses these paths, and
        # a correct reinstall needs setup.sh's staging/manifest handling.
        for src, rel in self.managed_tree_files():
            dst = self.claude_dir / rel
            if not dst.is_file():
                self.add("교정가능", rel, "missing", "run setup.sh to install managed skills/agents/commands")
            elif not self.installed_matches_source(src, dst):
                self.add("교정가능", rel, "drifted", "run setup.sh to reinstall managed skills/agents/commands")
            else:
                self.add("정상", rel, "installed", "none")

        self.check_overlay_parity()
        self.check_bridge_runtime_config()
        self.check_bridge_status()
        self.check_bridge_boot_path()
        self.check_continuation_state()
        self.check_memory_cache()
        self.check_nunchi_collection()
        self.check_cron_drift()
        self.check_provider_readiness()
        self.check_distill_readiness()
        # Managed Codex skills are provider-native (#647): diagnose them only on
        # a Codex node. Claude-only asset findings above stay non-readiness
        # (교정가능/정상), so they never block a Codex node's readiness.
        if self.provider == "codex":
            self.check_codex_managed_skills()

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

    def check_codex_managed_skills(self) -> None:
        """Diagnose repo-shipped managed Codex skills (#647), body-free.

        Reuses the read-only ``ccc_codex_skills.py plan`` contract so the doctor
        and the provisioner agree on CODEX_HOME safety, managed-skill presence,
        drift, and user-skill collisions. Never writes.
        """
        tool = self.repo / "scripts" / "ccc_codex_skills.py"
        if not tool.is_file():
            self.add(
                "교정가능",
                "managed Codex skills",
                "provisioning tool missing",
                "reinstall ccc-node from the repo",
            )
            return
        codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
        try:
            proc = subprocess.run(
                [
                    sys.executable, str(tool), "plan",
                    "--repo-root", str(self.repo),
                    "--codex-home", str(codex_home),
                ],
                text=True, capture_output=True,
                timeout=self.codex_probe_timeout(), check=False,
            )
        except subprocess.TimeoutExpired:
            self.add("수동필요", "managed Codex skills", "plan timed out", "rerun ccc-doctor")
            return
        except Exception:
            self.add("수동필요", "managed Codex skills", "plan failed", "rerun ccc-doctor")
            return

        if proc.returncode == 0:
            try:
                data = json.loads(proc.stdout)
            except ValueError:
                self.add("수동필요", "managed Codex skills", "malformed plan output", "rerun ccc-doctor")
                return
            skills = data.get("skills") or []
            pending = [s for s in skills if s.get("status") in ("create", "update")]
            if not pending:
                self.add("정상", "managed Codex skills", f"{len(skills)} installed, up to date", "none")
            else:
                names = ",".join(sorted(str(s.get("name", "?")) for s in pending))
                self.add(
                    "교정가능",
                    "managed Codex skills",
                    f"{len(pending)}/{len(skills)} to provision or update ({names})",
                    "run setup.sh from the repo to install managed Codex skills",
                )
            return

        stderr = (proc.stderr or "").strip()
        code = stderr.split()[-1] if stderr else "plan-error"
        if code in ("unsafe_codex_home", "unsafe_target"):
            self.add(
                "수동필요", "managed Codex skills", f"unsafe layout ({code})",
                "fix CODEX_HOME/skills to owner-only 0700 with no symlink, then rerun",
            )
        elif code == "unmanaged_collision":
            self.add(
                "교정가능", "managed Codex skills", "user skill name-collides with a managed skill",
                "rename the conflicting user skill, then run setup.sh",
            )
        elif code == "managed_drift":
            self.add(
                "교정가능", "managed Codex skills", "managed skill drifted",
                "run setup.sh from the repo to restore managed Codex skills",
            )
        else:
            self.add(
                "수동필요", "managed Codex skills", f"plan failed ({code})",
                "run scripts/ccc_codex_skills.py plan manually to inspect",
            )

    def check_provider_readiness(self) -> None:
        if self.provider == "claude":
            return
        if self.provider == "piri":
            if self._bridge_provider_state == ("piri", "healthy"):
                self.readiness = "ready"
                self.add("정상", "Piri runtime", "healthy", "none")
            else:
                self.readiness = "failed"
                self.add(
                    "수동필요",
                    "Piri runtime",
                    "live readiness not proven",
                    "inspect bridge status and Piri provider authentication",
                )
            return
        if self.provider != "codex":
            self.readiness = "failed"
            self.add(
                "수동필요",
                "agent provider",
                "unsupported provider",
                "set CCC_AGENT_PROVIDER to claude, codex, or piri",
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

    def check_distill_readiness(self) -> None:
        """Report extractor readiness separately without making a provider call."""

        configured = os.environ.get("CCC_MEMORY_DISTILL_PROVIDER", "auto").strip().lower()
        if configured == "off":
            self.distill_readiness = "disabled"
            self.add("정상", "distill extractor", "disabled", "none")
            return
        effective = self.provider if configured == "auto" else configured
        if effective not in {"claude", "codex", "piri"}:
            self.distill_readiness = "disabled" if configured == "auto" else "failed"
            self.add(
                "정상" if configured == "auto" else "수동필요",
                "distill extractor",
                f"configured={configured}; effective=off",
                (
                    "none"
                    if configured == "auto"
                    else "set CCC_MEMORY_DISTILL_PROVIDER to auto, off, claude, codex, or piri"
                ),
            )
            return
        piri_path = os.environ.get("CCC_PIRI_CLI_PATH", "").strip()
        if (
            effective == "piri"
            and not piri_path
            and self._bridge_provider_state == ("piri", "healthy")
        ):
            # The fleet sweep runs doctor from a clean login shell, while the
            # live bridge can receive its launcher path from systemd. Resolve
            # that path only for this static existence check. In particular,
            # do not import CCC_CODEX_CLI_PATH: the Codex readiness check runs
            # live probes and its service wrapper is not a probe-safe binary.
            piri_path = self.bridge_unit_environment_value("CCC_PIRI_CLI_PATH") or ""
        configured_paths = {
            "claude": os.environ.get("CLAUDE_CLI_PATH", "claude"),
            "codex": os.environ.get("CCC_CODEX_CLI_PATH", "codex"),
            "piri": piri_path or "piri",
        }
        raw = configured_paths[effective].strip()
        executable = None
        if raw:
            candidate = Path(raw).expanduser() if "/" in raw else None
            executable = (
                str(candidate)
                if candidate and candidate.is_file()
                else shutil.which(raw)
            )
        if not executable or not os.access(executable, os.X_OK):
            self.distill_readiness = "failed"
            self.add(
                "수동필요",
                "distill extractor",
                f"configured={configured}; effective={effective}; executable=missing",
                f"install/configure the {effective} CLI; no live authentication probe was attempted",
            )
            return
        same_live_runtime = self._bridge_provider_state == (effective, "healthy")
        codex_ready = (
            effective == "codex"
            and self.provider == "codex"
            and self.readiness == "ready"
        )
        if same_live_runtime or codex_ready:
            self.distill_readiness = "ready"
            self.add(
                "정상",
                "distill extractor",
                f"configured={configured}; effective={effective}; executable=available; shared runtime auth proven",
                "none",
            )
            return
        self.distill_readiness = "static-ready"
        self.add(
            "경고",
            "distill extractor",
            f"configured={configured}; effective={effective}; executable=available; live auth unproven",
            "run one explicitly approved body-free distill canary before production activation",
        )

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

    def check_bridge_runtime_config(self) -> None:
        """Validate body-free runtime invariants before app construction."""

        checker = self.repo / "bridge/runtime_config_check.py"
        if not checker.is_file():
            self.add(
                "경고",
                "bridge runtime config",
                "preflight unavailable",
                "restore bridge/runtime_config_check.py, then rerun ccc-doctor",
            )
            return
        project_root = Path(
            os.environ.get(
                "CCC_DOCTOR_BRIDGE_PROJECT_ROOT",
                self.running_bridge_home() or os.path.expanduser("~"),
            )
        ).expanduser()
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(checker),
                    "--project-root",
                    str(project_root),
                    "--bridge-env",
                    str(self.repo / "bridge/.env"),
                    "--json",
                ],
                text=True,
                capture_output=True,
                timeout=10,
            )
            payload = json.loads(completed.stdout)
        except Exception:
            self.add(
                "경고",
                "bridge runtime config",
                "preflight unreadable",
                "run bridge/runtime_config_check.py manually",
            )
            return
        code = str(payload.get("code", "invalid-runtime-settings"))
        if completed.returncode == 0 and payload.get("ok") is True:
            self.add("정상", "bridge runtime config", "valid", "none")
            return
        action = (
            "set CCC_DELEGATED_TASK_STALL_SECONDS below CLAUDE_PROCESS_TIMEOUT "
            "or restore the documented process-timeout default, then rerun"
            if code == "delegated-task-stall-not-lower-than-process-timeout"
            else "correct the named timeout setting, then rerun"
        )
        self.add("수동필요", "bridge runtime config", code, action)

    @staticmethod
    def bridge_status_verdict(returncode: int, output: str) -> tuple[str, str]:
        if returncode != 0:
            return "경고", f"probe-exit-{returncode}"
        if "Bot status: available" in output:
            return "정상", "available"
        if "Bot status: degraded" in output:
            return "경고", "degraded"
        if "Bot status: unavailable" in output:
            return "경고", "unavailable"
        return "경고", "unrecognized status output" if output.strip() else "no status output"

    @staticmethod
    def bridge_status_provider(output: str) -> tuple[str, str] | None:
        """Return the single body-free provider label rendered by start.sh."""

        labels = {
            "Claude": "claude",
            "Codex": "codex",
            "Piri": "piri",
        }
        found: list[tuple[str, str]] = []
        for label, provider in labels.items():
            match = re.search(
                rf"^\s*{label}:\s+(healthy|degraded|unavailable)\b",
                output,
                re.MULTILINE,
            )
            if match:
                found.append((provider, match.group(1)))
        return found[0] if len(found) == 1 else None

    def check_bridge_status(self) -> None:
        start = self.repo / "bridge/start.sh"
        if os.access(start, os.X_OK):
            # Probe the home the bridge actually serves, derived from the live
            # process. This was hardcoded to /root, so every non-root node
            # (Termux: $HOME=/data/data/com.termux/files/home) probed a home
            # nobody serves and reported a healthy bridge as "no status
            # output" (#771).
            probe_home = self.running_bridge_home() or os.path.expanduser("~")
            try:
                out = subprocess.run(["bash", str(start), "--path", probe_home, "--status"], text=True, capture_output=True, timeout=20)
                output = out.stdout + out.stderr
                detected_provider = self.bridge_status_provider(output)
                if detected_provider is not None:
                    self._bridge_provider_state = detected_provider
                    self.provider = detected_provider[0]
                klass, status = self.bridge_status_verdict(out.returncode, output)
                action = "none" if klass == "정상" else "inspect bridge service and body-free health diagnostics"
                self.add(klass, "bridge status", status, action)
            except Exception:
                self.add("경고", "bridge status", "no status output", "check bridge/start.sh manually if this node owns Telegram bridge")
        else:
            self.add("경고", "bridge status", "bridge/start.sh missing or not executable", "not all nodes run the Telegram bridge; install/check only if needed")

    @staticmethod
    def checkout_root_of(command: str) -> str | None:
        """Extract the ccc-node checkout a bridge command line runs from.

        Handles both deployed ExecStart shapes: `/bin/bash <root>/bridge/start.sh …`
        and `<root>/bridge/venv/bin/python -m telegram_bot …`.
        """
        marker = "/bridge/"
        for token in command.split():
            index = token.find(marker)
            if index > 0:
                return token[:index]
        return None

    def bridge_command_lines(self) -> list[str]:
        """Live bridge process command lines (the source of truth for paths)."""
        try:
            out = subprocess.run(
                ["ps", "-eo", "command"], text=True, capture_output=True, timeout=10
            ).stdout
        except Exception:
            return []
        return [
            line
            for line in out.splitlines()
            if "telegram_bot" in line and "--path" in line and "grep" not in line
        ]

    @staticmethod
    def bridge_home_of(command: str) -> str | None:
        """The --path (bridge home) a bridge command line serves."""
        tokens = command.split()
        for index, token in enumerate(tokens[:-1]):
            if token == "--path":
                return tokens[index + 1]
        return None

    def running_bridge_root(self) -> str | None:
        """The checkout the bridge is ACTUALLY serving from.

        Derived from the live process, never from a path guess: a node can hold
        several checkouts (/opt, /root, /home/<user>) and the first one found on
        disk is not necessarily the live one.
        """
        for line in self.bridge_command_lines():
            root = self.checkout_root_of(line)
            if root:
                return root
        return None

    def running_bridge_home(self) -> str | None:
        """The home the bridge serves, for probes that must target it."""
        for line in self.bridge_command_lines():
            home = self.bridge_home_of(line)
            if home:
                return home
        return None

    def unit_bridge_root(self, unit: Path) -> str | None:
        try:
            text = unit.read_text(errors="replace")
        except OSError:
            return None
        for line in text.splitlines():
            if line.startswith("ExecStart="):
                return self.checkout_root_of(line.split("=", 1)[1])
        return None

    @staticmethod
    def apply_systemd_environment(
        text: str, key: str, current: str | None = None
    ) -> str | None:
        """Apply one unit fragment's bounded Environment= semantics for key.

        Only the named key is retained; neighboring environment values (which
        may contain credentials) are never returned or rendered. ``shlex`` is
        sufficient for the quoted assignment forms used by generated units and
        node-local drop-ins. Malformed lines fail closed and are ignored.
        """

        value = current
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.startswith("Environment="):
                payload = line.split("=", 1)[1]
                if not payload:
                    value = None
                    continue
                try:
                    assignments = shlex.split(payload, posix=True)
                except ValueError:
                    continue
                for assignment in assignments:
                    name, separator, assigned = assignment.partition("=")
                    if separator and name == key:
                        value = assigned
            elif line.startswith("UnsetEnvironment="):
                payload = line.split("=", 1)[1]
                try:
                    names = shlex.split(payload, posix=True)
                except ValueError:
                    continue
                if any(name.partition("=")[0] == key for name in names):
                    value = None
        return value

    def bridge_systemd_units(self) -> list[Path]:
        """Known system and per-user unit paths, without invoking systemctl."""

        units = [Path("/etc/systemd/system/ccc-telegram-bridge.service")]
        units.append(
            Path.home() / ".config/systemd/user/ccc-telegram-bridge.service"
        )
        units.extend(
            sorted(
                Path("/home").glob(
                    "*/.config/systemd/user/ccc-telegram-bridge.service"
                )
            )
        )
        return list(dict.fromkeys(units))

    def bridge_unit_environment_value(self, key: str) -> str | None:
        """Read one value from the unit that owns this checkout's bridge.

        A stale twin checkout may have its own unit, so an Environment= value
        is trusted only when that unit's ExecStart matches the running bridge
        root (or this doctor checkout when the process path is unavailable).
        Main-unit and lexically ordered drop-in assignments are applied without
        exposing any other environment values.
        """

        expected_root = self.running_bridge_root() or str(self.repo.resolve())
        for unit in self.bridge_systemd_units():
            declared_root = self.unit_bridge_root(unit)
            if declared_root is None:
                continue
            if Path(declared_root).resolve() != Path(expected_root).resolve():
                continue
            value: str | None = None
            fragments = [unit]
            fragments.extend(sorted(unit.parent.glob(f"{unit.name}.d/*.conf")))
            for fragment in fragments:
                try:
                    text = fragment.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    continue
                value = self.apply_systemd_environment(text, key, value)
            if value:
                return value
        return None

    def check_bridge_boot_path(self) -> None:
        """The unit that starts the bridge must point at the live checkout (#55 follow-up).

        A unit left pointing at a stale twin checkout is silent: the running
        bridge is healthy, so nothing looks wrong, but the next reboot or
        `systemctl start` serves whatever that stale copy contains. Observed on
        yukson 2026-07-27 — an enabled unit aimed at a checkout 111 commits behind.
        """
        running = self.running_bridge_root()
        if running is None:
            self.add(
                "정상",
                "bridge boot path",
                "no bridge process; nothing to compare",
                "none",
            )
            return
        # The property under test is "whatever restarts the bridge points at the
        # live checkout" — systemd is only how Linux nodes implement it. Termux
        # nodes boot through Termux:Boot, so asking them for a unit reported a
        # correctly-booting node as unprotected (#771).
        if not self.has_systemd():
            self.check_bridge_boot_path_termux(running)
            return
        units = [Path("/etc/systemd/system/ccc-telegram-bridge.service")]
        units += sorted(
            Path("/home").glob("*/.config/systemd/user/ccc-telegram-bridge.service")
        )
        checked = 0
        for unit in units:
            declared = self.unit_bridge_root(unit)
            if declared is None:
                continue
            checked += 1
            if declared != running:
                self.add(
                    "수동필요",
                    "bridge boot path",
                    f"{unit} starts {declared} but the bridge runs from {running}",
                    "point ExecStart/WorkingDirectory at the running checkout, or "
                    "disable the unit; a reboot would otherwise serve the stale copy",
                )
                return
        if checked:
            self.add("정상", "bridge boot path", f"unit and runtime agree ({running})", "none")
        else:
            self.add(
                "경고",
                "bridge boot path",
                f"bridge runs from {running} but no unit declares it",
                "nothing restarts the bridge on reboot; install the systemd unit if this node should self-start",
            )

    def check_continuation_state(self) -> None:
        """Report the opt-in baton monitor and its owner-only state directory."""

        project_override = os.environ.get("CCC_DOCTOR_BRIDGE_PROJECT_ROOT")
        running_root = self.running_bridge_root()
        same_live_checkout = (
            running_root is not None
            and Path(running_root).resolve() == self.repo.resolve()
        )
        project_root = Path(
            project_override
            or (self.running_bridge_home() if same_live_checkout else None)
            or self.claude_dir.parent
        ).expanduser()
        state_dir = project_root / ".telegram_bot" / "continuation"
        configured = self.bridge_unit_environment_value(
            "CCC_CONTINUATION_ENABLED"
        ) or os.environ.get("CCC_CONTINUATION_ENABLED")
        enabled = configured is not None and configured.strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        configuration = "enabled" if enabled else "disabled (opt-in)"

        try:
            metadata = state_dir.lstat()
        except FileNotFoundError:
            self.add(
                "정상",
                "continuation state",
                f"configured={configuration}; state=not-created",
                "none",
            )
            return
        except OSError:
            self.add(
                "수동필요",
                "continuation state",
                f"configured={configuration}; state=unreadable",
                "inspect the bridge continuation state path without exposing queue contents",
            )
            return

        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            self.add(
                "수동필요",
                "continuation state",
                f"configured={configuration}; state=unsafe-type",
                "replace the continuation state path with an owner-only real directory",
            )
            return
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            self.add(
                "수동필요",
                "continuation state",
                f"configured={configuration}; state=wrong-owner",
                "restore process ownership before registering or cancelling continuations",
            )
            return
        if mode & 0o022:
            self.add(
                "수동필요",
                "continuation state",
                f"configured={configuration}; state=unsafe-mode-{mode:04o}",
                f"chmod 700 {state_dir} and inspect the creator before enabling continuation",
            )
            return
        self.add(
            "정상",
            "continuation state",
            f"configured={configuration}; state=private-{mode:04o}",
            "none",
        )

    @staticmethod
    def has_systemd() -> bool:
        """Whether this node booted with a systemd that could own the unit."""
        return Path("/run/systemd/system").is_dir()

    def declared_boot_root(self, line: str) -> str | None:
        """Checkout root a boot-script line starts the bridge from.

        Boot scripts reach the checkout through the shell (`"$HOME/ccc-node/…"`)
        rather than as a literal path, so quotes and $HOME are resolved before
        the shared extractor runs.
        """
        home = os.path.expanduser("~")
        cleaned = line.replace('"', " ").replace("'", " ")
        cleaned = cleaned.replace("${HOME}", home).replace("$HOME", home)
        return self.checkout_root_of(cleaned)

    def check_bridge_boot_path_termux(self, running: str) -> None:
        """Termux:Boot equivalent of the unit check — same property, same severity."""
        boot_dir = Path(os.path.expanduser("~/.termux/boot"))
        declared: list[tuple[Path, str]] = []
        if boot_dir.is_dir():
            for script in sorted(boot_dir.glob("*.sh")):
                try:
                    text = script.read_text(errors="replace")
                except OSError:
                    continue
                for line in text.splitlines():
                    root = self.declared_boot_root(line)
                    if root:
                        declared.append((script, root))
                        break
        for script, root in declared:
            if root != running:
                self.add(
                    "수동필요",
                    "bridge boot path",
                    f"{script} starts {root} but the bridge runs from {running}",
                    "point the Termux:Boot script at the running checkout, or "
                    "remove it; a reboot would otherwise serve the stale copy",
                )
                return
        if declared:
            self.add(
                "정상",
                "bridge boot path",
                f"Termux:Boot script and runtime agree ({running})",
                "none",
            )
        else:
            self.add(
                "경고",
                "bridge boot path",
                f"bridge runs from {running} but no Termux:Boot script starts it",
                "nothing restarts the bridge on reboot; add ~/.termux/boot/start-telegram-bridge.sh "
                "if this node should self-start",
            )

    def check_memory_cache(self) -> None:
        script = self.repo / "scripts/ccc-memory-check.sh"
        if os.access(script, os.X_OK):
            env = os.environ.copy()
            env.setdefault("CCC_STATE_DIR", str(self.claude_dir / "state"))
            env.setdefault("CCC_MEMORY_CACHE_DIR", str(self.claude_dir / "hooks/cache"))
            try:
                # Through an explicit bash, never as a bare executable: the
                # script's `#!/usr/bin/env bash` shebang is unresolvable on
                # Termux, so direct exec raised ENOENT and the whole memory
                # diagnostic silently degraded to "unavailable" (#775).
                out = subprocess.run(["bash", str(script), "--json"], text=True, capture_output=True, env=env, timeout=20).stdout.strip()
                mem = json.loads(out) if out else None
            except Exception:
                mem = None
            if isinstance(mem, dict):
                wiki = (mem.get("wiki") or {}).get("status", "unknown")
                honcho = (mem.get("honcho") or {}).get("status", "unknown")
                idx = (mem.get("local_index") or {}).get("exists", False)
                nunchi_payload = mem.get("nunchi") or {}
                nunchi = nunchi_payload.get("status", "unknown")
                mempalace = (mem.get("mempalace") or {}).get("status", "unknown")
                audiences = nunchi_payload.get("audience_scoped") or {}
                scoped_enabled = audiences.get("enabled") is True
                scoped_root = str(audiences.get("root_status", "disabled"))
                scoped_invalid = int(audiences.get("invalid_entries", 0) or 0)
                scoped_summary = ""
                if scoped_enabled:
                    scoped_summary = (
                        "; audiences={}/{}/{} root={} invalid={}".format(
                            int(audiences.get("scope_count", 0) or 0),
                            int(audiences.get("private_count", 0) or 0),
                            int(audiences.get("shared_count", 0) or 0),
                            scoped_root,
                            scoped_invalid,
                        )
                    )
                status = (
                    f"wiki={wiki}; honcho={honcho}; local_index={str(idx).lower()}; "
                    f"nunchi={nunchi}; mempalace={mempalace}{scoped_summary}"
                )
                if (
                    wiki in {"ok", "disabled"}
                    and honcho in {"ok", "disabled"}
                    and nunchi in {"ok", "off"}
                    and mempalace in {"ok", "off", "optional"}
                    and (
                        not scoped_enabled
                        or (scoped_root == "ok" and scoped_invalid == 0)
                    )
                ):
                    self.add("정상", "memory cache", status, "none")
                else:
                    self.add(
                        "경고",
                        "memory cache",
                        status,
                        "run scripts/ccc-memory-check.sh --json and inspect body-free cache/nunchi/mempalace diagnostics",
                    )
            else:
                self.add("경고", "memory cache", "diagnostic unavailable", "run scripts/ccc-memory-check.sh manually")
        else:
            self.add("경고", "memory cache", "ccc-memory-check.sh missing", "complete checkout or reinstall scripts")

    def check_nunchi_collection(self) -> None:
        """Surface the nunchi MemPalace collection lane for the live provider.

        Reports (body-free — paths/versions/state only, never transcript body,
        excerpts, session ids or credentials): the configured collection lane
        (from the managed cron) vs the runtime ``CCC_AGENT_PROVIDER`` (DRIFT),
        the source kind/path, the MemPalace binary/version (or peer-facts-only
        degrade), and the last collection state/exit/timestamp. The parsing
        mirrors ``install-nunchi.sh`` ``status`` (``_status_source`` /
        ``_status_collection`` / ``_status_provider_match``) so the doctor and
        the installer cannot diverge in semantics (#920). Severity is 정상 when
        the lane matches the runtime and the last run did not end in error;
        otherwise 경고 (warning, non-fatal — never flips the exit code)."""
        crontab_cmd = os.environ.get("CCC_CRONTAB_CMD", "crontab")
        try:
            cron = subprocess.run(
                [crontab_cmd, "-l"], text=True, capture_output=True, timeout=10
            ).stdout
        except Exception:
            cron = ""

        configured = "none"
        if re.search(r"codex-feed\.sh", cron):
            configured = "codex"
        elif re.search(r"piri-feed\.sh", cron):
            configured = "piri"
        elif re.search(r"ingest-cron\.sh", cron):
            configured = "claude"

        # Opt-in: no managed cron means nunchi collection is not wired — healthy.
        if configured == "none":
            self.add("정상", "nunchi collection", "not enabled (no managed cron)", "none")
            return

        # Source kind/path from the refresh cron line (mirrors _status_source).
        kind = "?"
        path = "(none)"
        m = re.search(r"mempalace-refresh\.sh\s+(\w+)\s+(.+?)\s*>>", cron)
        if m:
            kind = "mine" if m.group(1) in ("codex", "piri") else "sweep"
            path = m.group(2).strip()
            if len(path) >= 2 and path[0] in "\"'" and path[-1] == path[0]:
                path = path[1:-1]

        # Provider drift: configured cron lane vs the live runtime provider.
        match = "ok" if self.provider == configured else "DRIFT"

        # MemPalace binary + version (or peer-facts-only degrade).
        mp = os.environ.get("CCC_NUNCHI_MEMPALACE_CLI") or shutil.which("mempalace")
        if not mp and (Path.home() / ".local" / "bin" / "mempalace").exists():
            mp = str(Path.home() / ".local" / "bin" / "mempalace")
        mp_ok = bool(mp) and Path(mp).is_file() and os.access(mp, os.X_OK)
        version = "none"
        if mp_ok:
            with contextlib.suppress(Exception):
                out = subprocess.run(
                    [mp, "--version"], text=True, capture_output=True, timeout=10
                ).stdout
                version = (out.splitlines() or ["unknown"])[-1].strip() or "unknown"

        # Last body-free collection state (mirrors _status_collection).
        nunchi_home = Path(os.environ.get("NUNCHI_HOME", Path.home() / ".nunchi"))
        status_file = Path(
            os.environ.get(
                "CCC_NUNCHI_MEMPALACE_STATUS", nunchi_home / "mempalace-refresh.status.json"
            )
        )
        collection = "none"
        coll_state = ""
        try:
            data = json.loads(status_file.read_text())
            coll_state = str(data.get("state", ""))
            collection = "state={} exit_code={} finished_at={}".format(
                data.get("state", "?"),
                data.get("exit_code", "?"),
                data.get("finished_at", data.get("started_at", "?")),
            )
        except Exception:
            collection = "none"

        status = (
            "configured={}; runtime={}; match={}; source={} {}; "
            "mempalace={} version={}; collection={}"
        ).format(configured, self.provider, match, kind, path, mp or "missing", version, collection)

        healthy = match == "ok" and mp_ok and coll_state in {"ok", "running", ""}
        self.add(
            "정상" if healthy else "경고",
            "nunchi collection",
            status,
            "none"
            if healthy
            else "run scripts/install-nunchi.sh and align provider/source; "
            "install MemPalace for verbatim collection",
        )

    def installer_gen_stamp(self, installer: str) -> str | None:
        """Expected `gen=` stamp for one installer, via the shared helper.

        Shells out to ``scripts/lib/installer-gen-stamp.sh`` instead of
        reimplementing the digest, so apply-time stamping and doctor-time
        comparison share one implementation and cannot diverge (#1081 sibling
        of the #920 rule). None means the helper or installer is unreadable in
        this checkout, so the caller must degrade instead of guessing.
        """
        lib = self.repo / "scripts" / "lib" / "installer-gen-stamp.sh"
        script = self.repo / "scripts" / installer
        if not lib.is_file() or not script.is_file():
            return None
        try:
            proc = subprocess.run(
                ["bash", "-c", '. "$1" && ccc_installer_gen_stamp "$2"', "_", str(lib), str(script)],
                text=True,
                capture_output=True,
                timeout=10,
            )
        except Exception:
            return None
        out = proc.stdout.strip()
        if proc.returncode != 0 or not re.fullmatch(r"h_[0-9a-f]{12}", out):
            return None
        return out

    def check_cron_drift(self) -> None:
        """Surface frozen installer-managed cron entries (#1081 stage 1, PR-B).

        Installer-rendered cron lines are frozen at apply time: nothing re-runs
        the installer when the repo moves, so a merged fix never reaches the
        entry (#996 sat 4 days as a silent no-op; #1067 needed a manual
        10-node rollout). #1140 stamps each managed line with
        ``gen=h_<sha256:12>`` of the rendering installer; this check recomputes
        that stamp from the current checkout and compares.

        Stamp comparison only — never a re-render diff: the apply-time flags
        are unknown at check time, so re-rendering would report a false drift
        for any flag-customized entry (#996's emergency piri flags are the
        recorded example). Every finding is 경고 (non-fatal, exit code
        unchanged): stage 1 is visibility without behavior change. Body-free:
        marker names, stamp values and line counts only — never the command
        portion of a cron line.
        """
        crontab_cmd = os.environ.get("CCC_CRONTAB_CMD", "crontab")
        try:
            cron = subprocess.run(
                [crontab_cmd, "-l"], text=True, capture_output=True, timeout=10
            ).stdout
        except Exception:
            cron = ""

        managed: dict[str, list[str | None]] = {}
        ownerless: list[str] = []
        for raw_line in cron.splitlines():
            line = raw_line.strip()
            if not line or CRON_BLOCK_MARKER_RE.search(line):
                continue
            marker = next((m for m in INSTALLER_CRON_MARKERS if m in line), None)
            if marker is not None:
                stamp = CRON_GEN_STAMP_RE.search(line.split(marker, 1)[1])
                managed.setdefault(marker, []).append(stamp.group(1) if stamp else None)
                continue
            orphan = CRON_OWNERLESS_MARKER_RE.search(line)
            if orphan and orphan.group(0) not in ownerless:
                ownerless.append(orphan.group(0))

        # Opt-in: no managed marker at all means nothing was installed here.
        if not managed and not ownerless:
            self.add("정상", "installer cron entries", "none installed (opt-in)", "none")
            return

        for marker, gens in managed.items():
            installer = INSTALLER_CRON_MARKERS[marker]
            item = "cron entry {}".format(marker.lstrip("# "))
            expected = self.installer_gen_stamp(installer)
            if expected is None:
                self.add(
                    "경고", item, "cannot recompute stamp (installer or helper unreadable)",
                    f"restore scripts/{installer} and scripts/lib/installer-gen-stamp.sh "
                    "in this checkout, then rerun",
                )
                continue
            unstamped = sum(1 for gen in gens if gen is None)
            observed = sorted({gen for gen in gens if gen is not None})
            if unstamped:
                self.add(
                    "경고", item,
                    f"unstamped pre-#1081 entry ({unstamped}/{len(gens)} line(s))",
                    f"run scripts/{installer} --apply to re-render and stamp",
                )
            elif observed == [expected]:
                self.add("정상", item, f"gen match ({expected}, {len(gens)} line(s))", "none")
            else:
                self.add(
                    "경고", item, f"gen drift ({len(gens)} line(s))",
                    "run scripts/{} --apply (installed {} ≠ current {})".format(
                        installer, ",".join(observed), expected
                    ),
                )
        for token in ownerless:
            self.add(
                "경고", "cron entry {}".format(token.lstrip("# ")),
                "no installer in repo for this marker",
                "hand-installed or legacy entry — verify it is intended, then manage or "
                "remove it manually",
            )

    def print_report(self) -> None:
        print("# ccc doctor\n")
        print(f"- repo: `{self.repo}`")
        print(f"- harness version: `{self.harness_version()}`")
        print(f"- claude dir: `{self.claude_dir}`")
        print(f"- mode: `{self.mode}`")
        print(f"- provider: `{self.provider}`")
        print(f"- readiness: `{self.readiness}`")
        print(f"- distill readiness: `{self.distill_readiness}`\n")
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
            "distillReadiness": self.distill_readiness,
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
            for event in ("PostToolUse", "UserPromptSubmit", "Notification", "Stop", "SessionEnd"):
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
            created = subprocess.run(
                ["tar", "-czf", str(pre_archive), "-C", str(self.claude_dir), "settings.json"],
                check=False,
            ).returncode == 0
            if not created or not self.validate_settings_backup(pre_archive):
                pre_archive.unlink(missing_ok=True)
                print("failed to create valid pre-rollback settings backup; refusing rollback.", file=sys.stderr)
                return False
        restored = subprocess.run(
            ["tar", "-xzf", str(archive), "-C", str(self.claude_dir), "settings.json"],
            check=False,
        ).returncode == 0
        if not restored:
            print(f"failed to restore settings.json; recovery backup preserved at {pre_archive}", file=sys.stderr)
            return False
        print(f"applied settings.json rollback; restored={archive}; preRollbackBackup={pre_archive}")
        return True

    def file_repair_list(self) -> list[str]:
        out: list[str] = []
        groups: list[str] = []
        if self.scope_has("hooks"):
            groups += self.hook_files()
        if self.scope_has("output-styles"):
            groups += OUTPUT_STYLE_FILES
        for rel in groups:
            src = self.repo / "claude" / rel
            dst = self.claude_dir / rel
            if not dst.is_file() or (src.is_file() and not self.installed_matches_source(src, dst)):
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

    def validate_file_repair_backup(self, archive: Path, expected: list[str]) -> bool:
        if not archive.is_file():
            return False
        listing = subprocess.run(
            ["tar", "-tzf", str(archive)],
            capture_output=True,
            text=True,
            check=False,
        )
        if listing.returncode != 0:
            return False
        archived = {line.rstrip("/") for line in listing.stdout.splitlines() if line}
        return set(expected).issubset(archived)

    def backup_file_repairs(self, rels: list[str]) -> Path | None:
        ts = self.timestamp()
        backup_dir = self.claude_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        archive = backup_dir / f"ccc-doctor-files-{ts}.tar.gz"
        manifest = backup_dir / f"ccc-doctor-files-{ts}.manifest.txt"
        existing = [rel for rel in rels if (self.claude_dir / rel).exists()]
        if existing:
            with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as fh:
                for rel in existing:
                    fh.write(rel + "\n")
                list_path = fh.name
            try:
                created = subprocess.run(
                    ["tar", "-czf", str(archive), "-C", str(self.claude_dir), "-T", list_path],
                    check=False,
                ).returncode == 0
            finally:
                Path(list_path).unlink(missing_ok=True)
        else:
            created = subprocess.run(
                ["tar", "-czf", str(archive), "-C", str(self.claude_dir), "--files-from", "/dev/null", "--warning=no-file-changed"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode == 0
        if not created or not self.validate_file_repair_backup(archive, existing):
            archive.unlink(missing_ok=True)
            manifest.unlink(missing_ok=True)
            print("failed to create valid scoped file-repair backup; refusing repair.", file=sys.stderr)
            return None
        if not existing:
            manifest.write_text("no pre-existing files for scoped repair\n", encoding="utf-8")
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
        if archive is None:
            return False
        for rel in rels:
            src = self.repo / "claude" / rel
            dst = self.claude_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            # Reinstall the way setup.sh installs — a plain copy would drop the
            # canonical-path rewrite and point the repaired file at a checkout
            # that does not exist on a non-canonical node.
            self.install_source_file(src, dst)
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


def main(argv: list[str]) -> int:  # noqa: C901 -- #348 baseline hotspot
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
