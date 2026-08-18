from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
START_SH = ROOT / "start.sh"
DEPENDENCY_BOOTSTRAP = ROOT / "dependency_bootstrap.py"
# Service-install machinery extracted from start.sh (#584 P3-2).
SERVICE_SYSTEMD_SH = ROOT / "service-systemd.sh"
SERVICE_LAUNCHD_SH = ROOT / "service-launchd.sh"


def _start_text() -> str:
    return START_SH.read_text(encoding="utf-8")


class StartScriptStaticTests(unittest.TestCase):
    def test_start_sh_delegates_dependency_policy_to_python(self):
        text = _start_text()
        function_start = text.index("sync_dependencies()")
        function_end = text.index("get_checkout_version()", function_start)
        function_body = text[function_start:function_end]

        self.assertIn('"$SCRIPT_DIR/dependency_bootstrap.py"', function_body)
        self.assertIn('"--process-unlocked=$DEPS_UNLOCKED_PROCESS"', function_body)
        self.assertNotIn("pip install", function_body)
        self.assertNotIn("requirements.lock.txt", function_body)

    def test_dependency_bootstrap_owns_android_api_detection(self):
        text = DEPENDENCY_BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn("def ensure_android_api_level(", text)
        self.assertIn('"ro.build.version.sdk"', text)
        self.assertIn('if env.get("ANDROID_API_LEVEL")', text)

    def test_start_sh_never_logs_proxy_values(self):
        for script in (START_SH, SERVICE_SYSTEMD_SH, SERVICE_LAUNCHD_SH):
            text = script.read_text(encoding="utf-8")
            for variable in ("http_proxy", "https_proxy"):
                with self.subTest(script=script.name, variable=variable):
                    self.assertNotRegex(
                        text,
                        rf"(?m)^\s*echo[^\n]*\$(?:\{{)?{variable}(?:\}})?",
                    )

    def test_systemd_service_recovers_from_clean_process_exit(self):
        text = SERVICE_SYSTEMD_SH.read_text(encoding="utf-8")
        render_start = text.index("render_systemd_unit()")
        render_end = text.index("rendered_unit_value()", render_start)
        renderer = text[render_start:render_end]

        self.assertIn("Restart=always", renderer)
        self.assertNotIn("Restart=on-failure", renderer)

    def test_start_sh_dispatches_install_actions_to_subcommand_scripts(self):
        # The --install/--uninstall(-systemd) machinery lives in the extracted
        # subcommand scripts; start.sh must keep dispatching to them.
        #
        # `bash` is part of the pinned form, not incidental (#1161). Both
        # scripts declare `#!/bin/bash`, which resolves on neither Termux path
        # -- no /bin/bash, no /usr/bin/env -- so exec'ing them on their shebang
        # dies with 126. setup.sh's unguarded reconcile call did exactly that
        # and aborted the install, which rolled self-update back on every run
        # and pinned the node 21 commits behind main. Dropping the interpreter
        # here reintroduces that.
        text = _start_text()
        self.assertIn('exec bash "$SCRIPT_DIR/service-launchd.sh" install', text)
        self.assertIn('exec bash "$SCRIPT_DIR/service-launchd.sh" uninstall', text)
        self.assertIn('exec bash "$SCRIPT_DIR/service-systemd.sh" install', text)
        self.assertIn('exec bash "$SCRIPT_DIR/service-systemd.sh" uninstall', text)


if __name__ == "__main__":
    unittest.main()
