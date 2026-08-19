#!/usr/bin/env python3
"""Unit + baseline tests for ccc_script_interpreter_check.py (#1160).

Run directly (prints a PASS=<n> FAIL=<n> tally for the harness suite
contract) or via scripts/ccc-script-interpreter-check.test.sh.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MOD_PATH = ROOT / "scripts" / "ccc_script_interpreter_check.py"


def _load_mod():
    spec = importlib.util.spec_from_file_location("ccc_script_interpreter_check", MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CHK = _load_mod()


class ShellCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name)

    def sh(self, text: str):
        p = self.tmp / "fixture.sh"
        p.write_text(text)
        return CHK.check_shell_file(p, "fixture.sh")

    def assert_flags(self, text: str, count: int = 1):
        findings = self.sh(text)
        self.assertEqual(len(findings), count,
                         f"expected {count} finding(s), got {findings}")
        return findings

    def assert_clean(self, text: str):
        findings = self.sh(text)
        self.assertEqual(findings, [], f"expected no findings, got {findings}")

    # --- violations: the interpreter-less forms from #472/#663/#1151/#1157 ---
    def test_plain_command_position(self):
        self.assert_flags('"$HOOKDIR/scan-injection.sh" "$label"\n')

    def test_nohup_prefix(self):
        self.assert_flags('nohup "$SCRIPT_DIR/start.sh" >>"$log" 2>&1 &\n')

    def test_exec_prefix(self):
        self.assert_flags('exec "$DIR/foo.sh" --flag\n')

    def test_setsid_prefix(self):
        self.assert_flags('setsid "$DIR/foo.sh" </dev/null >/dev/null 2>&1 &\n')

    def test_after_pipe_inside_cmdsub(self):
        # the #1157 shape: scanner exec'd after a pipe in a substitution
        self.assert_flags('scanned="$(printf \'%s\' "$state" | "$HOOKDIR/scan.sh" lbl 2>/dev/null)"\n')

    def test_env_assignment_prefix(self):
        self.assert_flags('CCC_STORE="$s" \\\n  "$SRC/scripts/agent-cron.sh" list --json\n')

    def test_after_if(self):
        self.assert_flags('if "$D/probe.sh"; then echo ok; fi\n')

    def test_piped_into_loop(self):
        self.assert_flags('"$D/foo.sh" | while read -r l; do :; done\n')

    def test_literal_relative_path(self):
        self.assert_flags('./setup.sh --dry\n')

    def test_sudo_prefix(self):
        self.assert_flags('sudo "$D/foo.sh"\n')

    def test_backtick_cmdsub(self):
        self.assert_flags('out=`"$D/foo.sh" arg`\n')

    def test_continuation_line(self):
        self.assert_flags('nohup \\\n  "$D/start.sh" --daemon\n')

    def test_stderr_merge_keeps_head_detection(self):
        self.assert_flags('"$D/x.sh" 2>&1 | tee "$log"\n')

    def test_waiver_without_reason_still_flags(self):
        self.assert_flags('"$TMP/bad.sh" >/dev/null 2>&1  # ccc:interpreter-ok\n')

    # --- passes: interpreters, sourcing, arguments, seams, data ---
    def test_bash_named(self):
        self.assert_clean('bash "$D/foo.sh" --flag\n')

    def test_setsid_bash_named(self):
        # claude/hooks/lib/spawn-detached.sh is the canonical precedent
        self.assert_clean('setsid bash "$script" "$@" </dev/null >/dev/null 2>&1 &\n')

    def test_source_and_dot(self):
        self.assert_clean('. "$D/lib.sh"\nsource "$D/lib.sh"\n')

    def test_test_builtin_and_file_ops(self):
        self.assert_clean('[ -x "$D/foo.sh" ] && echo y\ncp "$D/foo.sh" "$dest/"\nchmod +x "$D/foo.sh"\n')

    def test_grep_and_sed_args(self):
        self.assert_clean('grep -q pattern "$D/foo.sh"\nsed -i "s|$HERE/start\\.sh |x |" "$unit"\n')

    def test_comment_line(self):
        self.assert_clean('# run "$D/foo.sh" after setup\n')

    def test_heredoc_body_is_data(self):
        self.assert_clean('cat > "$TMP/stub.sh" <<\'SH\'\n"$D/foo.sh" --runs-inside-the-stub\nSH\n')

    def test_assignments(self):
        self.assert_clean('SCAN="$D/scan.sh"\nlocal scan_bin="$D/scan.sh"\nexport TOOL="$D/tool.sh"\n')

    def test_array_literals(self):
        self.assert_clean('arr=(\n  "$A/x.sh"\n  "$B/y.sh"\n)\narr2=("$A/x.sh" "$B/y.sh")\n')

    def test_for_list_with_continuation(self):
        self.assert_clean('for f in \\\n  "$A/x.sh" \\\n  "$B/y.sh"; do\n  bash "$f"\ndone\n')

    def test_override_seams(self):
        self.assert_clean('exec "${CCC_BRIDGE_RESTART_SPAWN:-$D/start.sh}"\n"${CCC_SCAN_INJECTION_BIN:-$D/scan.sh}" "$label"\n')

    def test_launcher_array_head(self):
        # start.sh:989-1091 — launcher is (bash) for the repo default; an
        # explicit CCC_BRIDGE_RESTART_SPAWN execs on its own terms (the seam)
        self.assert_clean('local spawn_cmd="${CCC_BRIDGE_RESTART_SPAWN:-$SCRIPT_DIR/start.sh}"\n"${spawn_launcher[@]}" "$spawn_cmd" --daemon\n')

    def test_waiver_with_reason(self):
        self.assert_clean('"$TMP/bad.sh" >/dev/null 2>&1; rc=$?  # ccc:interpreter-ok: pins the exec-failure path (#1159)\n')

    def test_function_definition_and_bash_body(self):
        self.assert_clean('run_job() { bash "$WORKER" "$dir"; }\n')

    def test_cmdsub_without_script(self):
        self.assert_clean('out="$(cat "$f")"\n')

    def test_env_assignment_then_bash(self):
        self.assert_clean('FOO=bar bash "$D/x.sh"\n')

    def test_redirect_target_and_process_substitution(self):
        self.assert_clean('cat > "$TMP/tool.sh" <<\'SH\'\nexit 0\nSH\nwhile read -r x; do :; done < <(jq -r . "$f")\n')

    def test_multiline_quoted_cmdsub(self):
        # service-systemd.sh: the sed program inside a multi-line "$(...)" is data
        self.assert_clean('unit_path="$(systemctl show "$1" \\\n  | sed -n \'s|^ExecStart=/bin/bash \\(/.*\\)/bridge/start\\.sh --path /.*$|\\1|p\')"\n')

    def test_scanner_seam_variable(self):
        # checkpoint.sh: "$scan_bin" does not resolve to a literal .sh — the
        # default branch names bash, the override branch execs the seam as-is
        self.assert_clean('if [ -n "${CCC_SCAN_INJECTION_BIN:-}" ]; then\n  ckpt_run() { "$scan_bin" "$1"; }\nelse\n  ckpt_run() { bash "$scan_bin" "$1"; }\nfi\n')


class PythonCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name)

    def py(self, text: str):
        p = self.tmp / "fixture.py"
        p.write_text(text)
        return CHK.check_python_file(p, "fixture.py")

    def assert_flags(self, text: str, count: int = 1):
        findings = self.py(text)
        self.assertEqual(len(findings), count,
                         f"expected {count} finding(s), got {findings}")

    def assert_clean(self, text: str):
        findings = self.py(text)
        self.assertEqual(findings, [], f"expected no findings, got {findings}")

    def test_literal_head(self):
        self.assert_flags('import subprocess\nsubprocess.run(["/hooks/scan.sh", "lbl"])\n')

    def test_fstring_head(self):
        self.assert_flags('import subprocess\nsubprocess.Popen([f"{hooks}/search.sh", q])\n')

    def test_name_resolved_head(self):
        self.assert_flags('import subprocess\nTOOL = "hooks/search.sh"\nsubprocess.run([TOOL, q])\n')

    def test_os_path_join_head(self):
        self.assert_flags('import os, subprocess\nsubprocess.run([os.path.join(d, "foo.sh")])\n')

    def test_from_import_form(self):
        self.assert_flags('from subprocess import run\nrun(["/x/y.sh"])\n')

    def test_function_scope_name(self):
        self.assert_flags('import subprocess\ndef go(d):\n    tool = d + "/run.sh"\n    subprocess.run([tool])\n')

    def test_bash_head_passes(self):
        self.assert_clean('import subprocess\nsubprocess.run(["bash", tool, q])\n')

    def test_unresolvable_name_passes(self):
        # Popen([tool, q]) with tool a parameter: not statically a repo .sh —
        # the check stays silent rather than guess (#1159 was fixed at the
        # call site; this guard only fires on provable .sh heads)
        self.assert_clean('import subprocess\ndef go(tool):\n    subprocess.Popen([tool, q])\n')

    def test_system_tool_passes(self):
        self.assert_clean('import subprocess\nsubprocess.run(["tar", "-tzf", str(a)])\n')

    def test_bin_sh_c_passes(self):
        self.assert_clean('import subprocess\nsubprocess.run(["/bin/sh", "-c", CMD], input=t)\n')

    def test_waiver_with_reason(self):
        self.assert_clean('import subprocess\nsubprocess.run(["/x/y.sh"])  # ccc:interpreter-ok: fixture pins exec failure\n')

    def test_non_subprocess_attr_passes(self):
        self.assert_clean('import other\nother.run(["/x/y.sh"])\n')


class IntegrationCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name)

    def test_fixture_tree_exit_codes(self):
        (self.tmp / "bad.sh").write_text('"$D/foo.sh"\n')
        self.assertEqual(CHK.main(["--repo-root", str(self.tmp)]), 1)
        (self.tmp / "bad.sh").write_text('bash "$D/foo.sh"\n')
        self.assertEqual(CHK.main(["--repo-root", str(self.tmp)]), 0)

    def test_missing_repo_root(self):
        self.assertEqual(CHK.main(["--repo-root", str(self.tmp / "nope")]), 2)

    def test_real_repo_baseline_is_clean(self):
        # The repo itself must stay at 0 findings — this is the baseline the
        # issue asked for before the check could fail CI on new violations.
        findings, scanned, _ = CHK.run_check(ROOT)
        self.assertGreater(scanned, 100)
        self.assertEqual(findings, [],
                         "repo baseline must be clean:\n" + "\n".join(map(str, findings)))


def main() -> int:
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite()
    for case in (ShellCase, PythonCase, IntegrationCase):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    failed = len(result.failures) + len(result.errors)
    print(f"PASS={result.testsRun - failed} FAIL={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
