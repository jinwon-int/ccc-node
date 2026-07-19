from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
START_SH = ROOT / "start.sh"
# Service-install machinery extracted from start.sh (#584 P3-2).
SERVICE_SYSTEMD_SH = ROOT / "service-systemd.sh"
SERVICE_LAUNCHD_SH = ROOT / "service-launchd.sh"


def _start_text() -> str:
    return START_SH.read_text(encoding="utf-8")


class StartScriptStaticTests(unittest.TestCase):
    def test_start_sh_auto_detects_android_api_level_before_pip_install(self):
        text = _start_text()
        self.assertIn("ensure_android_api_level()", text)
        self.assertIn("getprop ro.build.version.sdk", text)
        self.assertIn("export ANDROID_API_LEVEL", text)

        install = text.index("📦 Installing Python dependencies")
        detect = text.index("ensure_android_api_level", install)
        upgrade = text.index("install -q --upgrade pip", install)
        self.assertLess(detect, upgrade)

    def test_start_sh_preserves_operator_provided_android_api_level(self):
        text = _start_text()
        function_start = text.index("ensure_android_api_level()")
        function_end = text.index("sync_dependencies()", function_start)
        function_body = text[function_start:function_end]
        self.assertIn('if [ -n "${ANDROID_API_LEVEL:-}" ]; then', function_body)
        self.assertIn("return 0", function_body)

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
        install_start = text.index("do_install_systemd()")
        install_end = text.index("do_uninstall_systemd()", install_start)
        installer = text[install_start:install_end]

        self.assertIn("Restart=always", installer)
        self.assertNotIn("Restart=on-failure", installer)

    def test_start_sh_dispatches_install_actions_to_subcommand_scripts(self):
        # The --install/--uninstall(-systemd) machinery lives in the extracted
        # subcommand scripts; start.sh must keep dispatching to them.
        text = _start_text()
        self.assertIn('exec "$SCRIPT_DIR/service-launchd.sh" install', text)
        self.assertIn('exec "$SCRIPT_DIR/service-launchd.sh" uninstall', text)
        self.assertIn('exec "$SCRIPT_DIR/service-systemd.sh" install', text)
        self.assertIn('exec "$SCRIPT_DIR/service-systemd.sh" uninstall', text)


if __name__ == "__main__":
    unittest.main()
