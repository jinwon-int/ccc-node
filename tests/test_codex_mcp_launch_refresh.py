"""codex/mcp-launch-refresh.py — Codex web MCP npx -> node <global cli> (#2543).

Claude's lane re-pins registered web MCP launches through
``claude/mcp-setup.sh --web-launch-only`` (#2300/#2315); this pins the Codex
counterpart for ``~/.codex/config.toml``. Every case runs against a temp
CODEX_HOME and a fake global npm root — never the node's real config or npm.
"""

from __future__ import annotations

import importlib.util
import io
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "codex" / "mcp-launch-refresh.py"

_spec = importlib.util.spec_from_file_location("codex_mcp_launch_refresh", SCRIPT)
assert _spec is not None and _spec.loader is not None
mlr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mlr)

FAKE_KEY = "fc-FAKE2543-test-key"

NPX_CONFIG = f"""# node-local Codex config
model = "gpt-5"

[mcp_servers.searxng]
command = "npx"
args = ["-y", "mcp-searxng"]

[mcp_servers.searxng.env]
SEARXNG_URL = "http://searx.invalid:8080"

[mcp_servers.firecrawl]
command = "npx"   # pinned by mcp-setup
args = ["-y", "firecrawl-mcp@latest"]
env = {{ FIRECRAWL_API_KEY = "{FAKE_KEY}" }}

[projects."/root"]
trust_level = "trusted"
"""


def _install(root: Path, pkg: str, bin_field, rel: str) -> Path:
    pkg_dir = root / pkg
    cli = (pkg_dir / rel).resolve()
    cli.parent.mkdir(parents=True, exist_ok=True)
    cli.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    import json

    (pkg_dir / "package.json").write_text(
        json.dumps({"name": pkg, "bin": bin_field}), encoding="utf-8"
    )
    return cli


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.codex = self.tmp / "codex"
        self.codex.mkdir(mode=0o700)
        self.config = self.codex / "config.toml"
        self.backups = self.tmp / "claude" / "state" / "backups"
        self.groot = self.tmp / "gmodules"
        self.groot.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def write_config(self, text: str) -> bytes:
        self.config.write_text(text, encoding="utf-8")
        os.chmod(self.config, 0o600)
        return self.config.read_bytes()

    def install_all(self, root: Path | None = None) -> dict[str, Path]:
        root = root or self.groot
        return {
            "searxng": _install(root, "mcp-searxng", {"mcp-searxng": "dist/index.js"}, "dist/index.js"),
            "firecrawl": _install(root, "firecrawl-mcp", {"firecrawl-mcp": "dist/index.js"}, "dist/index.js"),
            "context7": _install(root, "@upstash/context7-mcp", "dist/index.js", "dist/index.js"),
        }

    def run_refresh(self, *, npm_root: str | None = None, dry_run: bool = False):
        out, err = io.StringIO(), io.StringIO()
        rc = mlr.refresh(
            self.codex,
            backup_dir=self.backups,
            npm_root=str(self.groot) if npm_root is None else npm_root,
            dry_run=dry_run,
            out=out,
            err=err,
        )
        return rc, out.getvalue(), err.getvalue()

    def backups_written(self):
        return sorted(self.backups.glob("codex-config.toml.before-mcp-pin-*")) if self.backups.exists() else []


class ConvertTests(_Base):
    def test_npx_blocks_converted_rest_byte_identical(self):
        clis = self.install_all()
        original = self.write_config(NPX_CONFIG)
        rc, out, err = self.run_refresh()
        self.assertEqual(rc, 0, err)
        new = self.config.read_text(encoding="utf-8")
        parsed = tomllib.loads(new)
        self.assertEqual(parsed["mcp_servers"]["searxng"]["command"], "node")
        self.assertEqual(parsed["mcp_servers"]["searxng"]["args"], [str(clis["searxng"])])
        self.assertEqual(parsed["mcp_servers"]["firecrawl"]["args"], [str(clis["firecrawl"])])
        # Only the four launch lines differ; comments and everything else kept.
        old_lines = original.decode().splitlines()
        new_lines = new.splitlines()
        self.assertEqual(len(old_lines), len(new_lines))
        changed = [(a, b) for a, b in zip(old_lines, new_lines) if a != b]
        self.assertEqual(len(changed), 4)
        self.assertIn('command = "node"   # pinned by mcp-setup', new_lines)
        # Unregistered context7 is not added even though it is installed.
        self.assertNotIn("context7", parsed["mcp_servers"])
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        self.assertIn("searxng: re-pinned to node", out)

    def test_backup_holds_original_and_is_private(self):
        self.install_all()
        original = self.write_config(NPX_CONFIG)
        self.run_refresh()
        backups = self.backups_written()
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
        # A second same-day run with something to change never overwrites it.
        self.write_config(NPX_CONFIG)
        self.run_refresh()
        self.assertEqual(len(self.backups_written()), 2)
        self.assertEqual(backups[0].read_bytes(), original)

    def test_env_preserved_and_never_printed(self):
        self.install_all()
        self.write_config(NPX_CONFIG)
        before = tomllib.loads(NPX_CONFIG)["mcp_servers"]
        rc, out, err = self.run_refresh()
        self.assertEqual(rc, 0)
        after = tomllib.loads(self.config.read_text(encoding="utf-8"))["mcp_servers"]
        self.assertEqual(after["firecrawl"]["env"], before["firecrawl"]["env"])
        self.assertEqual(after["searxng"]["env"], before["searxng"]["env"])
        self.assertNotIn(FAKE_KEY, out + err)
        self.assertNotIn("searx.invalid", out + err)

    def test_scoped_package_with_string_bin(self):
        clis = self.install_all()
        self.write_config('[mcp_servers.context7]\ncommand = "npx"\nargs = ["-y", "@upstash/context7-mcp@1.0.14"]\n')
        rc, _out, err = self.run_refresh()
        self.assertEqual(rc, 0, err)
        parsed = tomllib.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(parsed["mcp_servers"]["context7"], {"command": "node", "args": [str(clis["context7"])]})

    def test_crlf_line_endings_kept(self):
        self.install_all()
        self.write_config("")
        self.config.write_bytes(b'[mcp_servers.searxng]\r\ncommand = "npx"\r\nargs = ["-y", "mcp-searxng"]\r\n')
        rc, _out, err = self.run_refresh()
        self.assertEqual(rc, 0, err)
        data = self.config.read_bytes()
        self.assertEqual(data.count(b"\r\n"), 3)
        self.assertIn(b'command = "node"\r\n', data)


class UnchangedTests(_Base):
    def assert_untouched(self, original: bytes):
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.backups_written(), [])

    def test_already_node_unchanged(self):
        clis = self.install_all()
        original = self.write_config(
            f'[mcp_servers.searxng]\ncommand = "node"\nargs = ["{clis["searxng"]}"]\n'
        )
        rc, out, _err = self.run_refresh()
        self.assertEqual(rc, 0)
        self.assert_untouched(original)
        self.assertIn("already node", out)

    def test_custom_commands_unchanged(self):
        self.install_all()
        original = self.write_config(
            '[mcp_servers.searxng]\ncommand = "/usr/local/bin/mcp-searxng"\nargs = []\n\n'
            '[mcp_servers.firecrawl]\ncommand = "npx"\nargs = ["-y", "firecrawl-mcp", "--verbose"]\n\n'
            '[mcp_servers.context7]\ncommand = "npx"\nargs = ["-y", "some-fork-of-context7"]\n\n'
            '[mcp_servers.other]\ncommand = "npx"\nargs = ["-y", "mcp-searxng"]\n'
        )
        rc, out, _err = self.run_refresh()
        self.assertEqual(rc, 0)
        self.assert_untouched(original)
        self.assertEqual(out.count("custom launch"), 3)

    def test_not_installed_globally_skipped(self):
        original = self.write_config(NPX_CONFIG)  # nothing installed in groot
        rc, out, _err = self.run_refresh()
        self.assertEqual(rc, 0)
        self.assert_untouched(original)
        self.assertIn("searxng: SKIPPED — mcp-searxng is not installed globally", out)
        self.assertIn("firecrawl: SKIPPED", out)

    def test_bin_pointing_at_missing_file_is_not_installed(self):
        (self.groot / "mcp-searxng").mkdir()
        (self.groot / "mcp-searxng" / "package.json").write_text('{"bin": "dist/missing.js"}')
        original = self.write_config('[mcp_servers.searxng]\ncommand = "npx"\nargs = ["-y", "mcp-searxng"]\n')
        rc, out, _err = self.run_refresh()
        self.assertEqual(rc, 0)
        self.assert_untouched(original)
        self.assertIn("SKIPPED", out)

    def test_unrecognized_layout_left_untouched(self):
        self.install_all()
        original = self.write_config(
            '[mcp_servers]\nsearxng = { command = "npx", args = ["-y", "mcp-searxng"] }\n\n'
            '[mcp_servers.firecrawl]\ncommand = "npx"\nargs = [\n  "-y",\n  "firecrawl-mcp",\n]\n'
        )
        rc, out, _err = self.run_refresh()
        self.assertEqual(rc, 0)
        self.assert_untouched(original)
        self.assertEqual(out.count("layout not recognized"), 2)

    def test_npm_not_spawned_without_an_npx_candidate(self):
        original = self.write_config('[mcp_servers.searxng]\ncommand = "node"\nargs = ["/x/cli.js"]\n')
        with mock.patch.object(mlr, "npm_global_roots", side_effect=AssertionError("npm spawned")):
            rc, _out, err = self.run_refresh(npm_root="")
        self.assertEqual(rc, 0, err)
        self.assert_untouched(original)

    def test_no_config_is_a_noop(self):
        rc, out, _err = self.run_refresh()
        self.assertEqual(rc, 0)
        self.assertIn("no config.toml", out)
        self.assertFalse(self.config.exists())

    def test_symlinked_config_refused(self):
        self.install_all()
        real = self.tmp / "real.toml"
        real.write_text(NPX_CONFIG, encoding="utf-8")
        self.config.symlink_to(real)
        rc, _out, err = self.run_refresh()
        self.assertEqual(rc, 1)
        self.assertIn("not a regular file", err)
        self.assertEqual(real.read_text(encoding="utf-8"), NPX_CONFIG)


class RollbackTests(_Base):
    def test_unparsable_original_left_untouched(self):
        self.install_all()
        original = self.write_config(NPX_CONFIG + "\n[broken\n")
        rc, out, err = self.run_refresh()
        self.assertEqual(rc, 1)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertIn("does not parse", err)
        self.assertNotIn(FAKE_KEY, out + err)

    def test_edit_that_fails_reparse_is_rolled_back_before_write(self):
        self.install_all()
        original = self.write_config(NPX_CONFIG)
        with mock.patch.object(mlr, "_render_args_line", lambda indent, cli, suffix: f'{indent}args = ["{cli}"'):
            rc, out, err = self.run_refresh()
        self.assertEqual(rc, 1)
        self.assertIn("failed to re-parse — rolled back", err)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.backups_written(), [])
        self.assertNotIn(FAKE_KEY, out + err)

    def test_edit_touching_other_keys_is_rolled_back(self):
        self.install_all()
        original = self.write_config(NPX_CONFIG)
        with mock.patch.object(mlr, "_render_command_line", lambda indent, suffix: f'{indent}command = "node"\nextra = 1{suffix}'):
            rc, _out, err = self.run_refresh()
        self.assertEqual(rc, 1)
        self.assertIn("differs beyond the launch lines", err)
        self.assertEqual(self.config.read_bytes(), original)

    def test_written_file_failing_verification_is_restored(self):
        self.install_all()
        original = self.write_config(NPX_CONFIG)
        real_write = mlr.write_atomic
        calls = []

        def corrupting_write(path, payload, like, expected_sig):
            calls.append(payload)
            if len(calls) == 1:
                payload = payload + b"\n[broken\n"
            return real_write(path, payload, like, expected_sig)

        with mock.patch.object(mlr, "write_atomic", corrupting_write):
            rc, out, err = self.run_refresh()
        self.assertEqual(rc, 1)
        self.assertEqual(len(calls), 2)
        self.assertIn("original restored", err)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        backups = self.backups_written()
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertNotIn(FAKE_KEY, out + err)


class DryRunTests(_Base):
    def test_dry_run_writes_nothing_and_names_only(self):
        self.install_all()
        original = self.write_config(NPX_CONFIG)
        rc, out, err = self.run_refresh(dry_run=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse(self.backups.exists())
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(
            lines,
            [
                "  - Codex searxng: would re-pin npx -> node <global cli>",
                "  - Codex firecrawl: would re-pin npx -> node <global cli>",
            ],
        )
        self.assertNotIn(str(self.groot), out)
        self.assertNotIn(FAKE_KEY, out + err)

    def test_dry_run_nothing_to_change(self):
        original = self.write_config(NPX_CONFIG)
        rc, out, _err = self.run_refresh(dry_run=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertIn("no web MCP launch would change", out)


def _fake_npm(bin_dir: Path, body: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    npm = bin_dir / "npm"
    npm.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    npm.chmod(0o755)


class NpmRootTests(_Base):
    """Runtime-user npm root (ND-4262) and Termux prefixes, via a fake npm."""

    def _env(self, home: Path, bin_dir: Path, **extra):
        env = {"HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin"}
        env.update(extra)
        return mock.patch.dict(os.environ, env, clear=False)

    def test_runtime_user_npm_root_from_npmrc_home(self):
        home = self.tmp / "home-runtime-user"
        user_root = home / ".local" / "lib" / "node_modules"
        clis = self.install_all(user_root)
        bin_dir = self.tmp / "bin"
        # npm answers from the HOME it runs with (stands in for ~/.npmrc prefix).
        _fake_npm(bin_dir, '[ "$1 $2" = "root -g" ] && { echo "$HOME/.local/lib/node_modules"; exit 0; }\nexit 1\n')
        self.write_config(NPX_CONFIG)
        with self._env(home, bin_dir):
            rc, _out, err = self.run_refresh(npm_root="")
        self.assertEqual(rc, 0, err)
        parsed = tomllib.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(parsed["mcp_servers"]["searxng"]["args"], [str(clis["searxng"])])

    def test_local_prefix_fallback_when_npm_root_lacks_package(self):
        home = self.tmp / "home-user"
        clis = self.install_all(home / ".local" / "lib" / "node_modules")
        bin_dir = self.tmp / "bin"
        system_root = self.tmp / "usr-lib-node_modules"
        system_root.mkdir()
        _fake_npm(bin_dir, f'echo "{system_root}"\n')
        self.write_config(NPX_CONFIG)
        with self._env(home, bin_dir):
            roots = mlr.npm_global_roots(str(home))
            rc, _out, err = self.run_refresh(npm_root="")
        self.assertEqual(roots, [str(system_root), str(home / ".local" / "lib" / "node_modules")])
        self.assertEqual(rc, 0, err)
        parsed = tomllib.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(parsed["mcp_servers"]["firecrawl"]["args"], [str(clis["firecrawl"])])

    def test_runtime_home_is_config_owner_home(self):
        # Owned by the caller -> caller's HOME.
        self.write_config(NPX_CONFIG)
        with mock.patch.dict(os.environ, {"HOME": "/home/caller"}):
            self.assertEqual(mlr.runtime_home(self.config), "/home/caller")
        # Owned by someone else (root running on a dual-domain node) -> owner's home.
        fake = os.stat_result((stat.S_IFREG | 0o600, 0, 0, 1, os.geteuid() + 4242, 0, 0, 0, 0, 0))
        pw = mock.Mock(pw_dir="/home/runtime")
        with mock.patch.object(Path, "lstat", return_value=fake), \
                mock.patch.object(mlr.pwd, "getpwuid", return_value=pw) as getpw:
            self.assertEqual(mlr.runtime_home(self.config), "/home/runtime")
        getpw.assert_called_once_with(os.geteuid() + 4242)

    def test_other_users_npm_env_drops_inherited_npm_config(self):
        with mock.patch.dict(os.environ, {"HOME": "/root", "NPM_CONFIG_PREFIX": "/root/.npm-global",
                                          "npm_config_cache": "/root/.npm"}):
            env = mlr._npm_env("/home/runtime")
        self.assertEqual(env["HOME"], "/home/runtime")
        self.assertNotIn("NPM_CONFIG_PREFIX", env)
        self.assertNotIn("npm_config_cache", env)

    def test_termux_prefix_path(self):
        prefix = self.tmp / "data" / "data" / "com.termux" / "files" / "usr"
        troot = prefix / "lib" / "node_modules"
        clis = self.install_all(troot)
        bin_dir = prefix / "bin"
        _fake_npm(bin_dir, 'echo "$PREFIX/lib/node_modules"\n')
        self.write_config(NPX_CONFIG)
        home = self.tmp / "data" / "data" / "com.termux" / "files" / "home"
        home.mkdir(parents=True)
        with self._env(home, bin_dir, PREFIX=str(prefix)):
            rc, out, err = self.run_refresh(npm_root="")
        self.assertEqual(rc, 0, err)
        parsed = tomllib.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(parsed["mcp_servers"]["searxng"]["command"], "node")
        self.assertEqual(parsed["mcp_servers"]["searxng"]["args"], [str(clis["searxng"])])
        self.assertTrue(parsed["mcp_servers"]["searxng"]["args"][0].startswith(str(troot) + os.sep))
        self.assertIn("com.termux/files/usr/lib/node_modules/mcp-searxng", out)

    def test_cli_path_needing_escapes_round_trips(self):
        root = self.tmp / 'odd "dir" \\ name'
        clis = self.install_all(root)
        self.write_config('[mcp_servers.searxng]\ncommand = "npx"\nargs = ["-y", "mcp-searxng"]\n')
        rc, _out, err = self.run_refresh(npm_root=str(root))
        self.assertEqual(rc, 0, err)
        parsed = tomllib.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(parsed["mcp_servers"]["searxng"]["args"], [str(clis["searxng"])])


class CliTests(_Base):
    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--codex-home", str(self.codex),
             "--backup-dir", str(self.backups), "--npm-root", str(self.groot), *args],
            capture_output=True, text=True, check=False,
        )

    def test_cli_dry_run_then_apply(self):
        self.install_all()
        original = self.write_config(NPX_CONFIG)
        dry = self._run("--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertIn("searxng: would re-pin", dry.stdout)
        real = self._run()
        self.assertEqual(real.returncode, 0, real.stderr)
        self.assertNotIn(FAKE_KEY, real.stdout + real.stderr)
        again = self._run()
        self.assertEqual(again.returncode, 0)
        self.assertIn("already node", again.stdout)
        self.assertEqual(len(self.backups_written()), 1)

    def test_cli_unknown_flag_is_usage_error(self):
        self.assertEqual(self._run("--bogus").returncode, 2)


if __name__ == "__main__":
    unittest.main()
