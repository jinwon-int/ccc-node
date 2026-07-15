import importlib
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pydantic import ValidationError


class VoiceProviderConfigTests(unittest.TestCase):
    def _load_config_module(self, project_root: str):
        with patch.dict(
            os.environ,
            {
                "PROJECT_ROOT": project_root,
                "TELEGRAM_BOT_TOKEN": "123456:abc",
                "TRANSCRIPTION_PROVIDER": "whisper",
            },
            clear=True,
        ):
            sys.modules.pop("telegram_bot.utils.config", None)
            return importlib.import_module("telegram_bot.utils.config")

    def test_default_provider_is_whisper(self):
        with TemporaryDirectory() as td:
            module = self._load_config_module(td)
            cfg = module.Config(telegram_bot_token="123456:abc", _env_file=None)
            self.assertEqual(cfg.transcription_provider, "whisper")

    def test_execution_profile_defaults_to_strict_project(self):
        with TemporaryDirectory() as td:
            module = self._load_config_module(td)
            cfg = module.Config(telegram_bot_token="123456:abc", _env_file=None)
            self.assertEqual(cfg.execution_profile, "strict-project")

    def test_execution_profile_reads_explicit_env(self):
        with TemporaryDirectory() as td:
            module = self._load_config_module(td)
            with patch.dict(
                os.environ,
                {"CCC_BRIDGE_EXECUTION_PROFILE": "owner-operator"},
                clear=False,
            ):
                cfg = module.Config(telegram_bot_token="123456:abc", _env_file=None)
            self.assertEqual(cfg.execution_profile, "owner-operator")

    def test_shared_group_memory_and_image_guards_are_explicit_opt_ins(self):
        with TemporaryDirectory() as td:
            module = self._load_config_module(td)
            cfg = module.Config(telegram_bot_token="123456:abc", _env_file=None)
            self.assertEqual(cfg.telegram_session_scope, "per-user-chat")
            self.assertEqual(cfg.bridge_memory_mode, "off")
            self.assertFalse(cfg.image_context_guard)

            with patch.dict(
                os.environ,
                {
                    "CCC_TELEGRAM_SESSION_SCOPE": "shared-groups",
                    "CCC_BRIDGE_MEMORY_MODE": "curated",
                    "CCC_BRIDGE_IMAGE_CONTEXT_GUARD": "true",
                    "CCC_TELEGRAM_MAX_IMAGE_BYTES": "1048576",
                    "CCC_TELEGRAM_MAX_IMAGE_PIXELS": "1000000",
                },
                clear=False,
            ):
                enabled = module.Config(telegram_bot_token="123456:abc", _env_file=None)
            self.assertEqual(enabled.telegram_session_scope, "shared-groups")

            shared_all = module.Config(
                telegram_bot_token="123456:abc",
                CCC_TELEGRAM_SESSION_SCOPE="shared-all",
                _env_file=None,
            )
            self.assertEqual(shared_all.telegram_session_scope, "shared-all")
            self.assertEqual(enabled.bridge_memory_mode, "curated")
            self.assertTrue(enabled.image_context_guard)
            self.assertEqual(enabled.telegram_max_image_bytes, 1048576)
            self.assertEqual(enabled.telegram_max_image_pixels, 1000000)

    def test_execution_profile_precedence_in_fresh_processes(self):
        source_config = Path(__file__).resolve().parents[1] / "utils" / "config.py"
        with TemporaryDirectory() as td:
            root = Path(td)
            package_root = root / "package"
            package = package_root / "telegram_bot"
            utils = package / "utils"
            utils.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            (utils / "__init__.py").write_text("", encoding="utf-8")
            shutil.copy2(source_config, utils / "config.py")

            project_root = root / "project"
            project_env = project_root / ".telegram_bot" / ".env"
            project_env.parent.mkdir(parents=True)
            package_env = package / ".env"

            def fresh_profile(
                *,
                process_value=None,
                project_value=None,
                package_value=None,
            ):
                project_lines = ["TELEGRAM_BOT_TOKEN=123456:test"]
                if project_value is not None:
                    project_lines.append(f"CCC_BRIDGE_EXECUTION_PROFILE={project_value}")
                project_env.write_text("\n".join(project_lines) + "\n", encoding="utf-8")
                package_lines = []
                if package_value is not None:
                    package_lines.append(f"CCC_BRIDGE_EXECUTION_PROFILE={package_value}")
                package_env.write_text("\n".join(package_lines) + "\n", encoding="utf-8")

                env = {
                    "HOME": str(root / "home"),
                    "PATH": os.environ.get("PATH", ""),
                    "PROJECT_ROOT": str(project_root),
                    "PYTHONPATH": str(package_root),
                }
                if process_value is not None:
                    env["CCC_BRIDGE_EXECUTION_PROFILE"] = process_value
                result = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "from telegram_bot.utils.config import config; "
                        "print(config.execution_profile)",
                    ],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=True,
                )
                return result.stdout.strip()

            self.assertEqual(
                fresh_profile(
                    process_value="disabled",
                    project_value="strict-project",
                    package_value="owner-operator",
                ),
                "disabled",
            )
            self.assertEqual(
                fresh_profile(project_value="strict-project", package_value="owner-operator"),
                "strict-project",
            )
            self.assertEqual(
                fresh_profile(package_value="owner-operator"),
                "owner-operator",
            )
            self.assertEqual(fresh_profile(), "strict-project")

    def test_invalid_provider_is_rejected(self):
        with TemporaryDirectory() as td:
            module = self._load_config_module(td)
            with self.assertRaises(ValidationError):
                module.Config(
                    telegram_bot_token="123456:abc",
                    transcription_provider="invalid-provider",
                    _env_file=None,
                )

    def test_volcengine_provider_requires_credentials(self):
        with TemporaryDirectory() as td:
            module = self._load_config_module(td)
            with self.assertRaises(ValidationError):
                module.Config(
                    telegram_bot_token="123456:abc",
                    transcription_provider="volcengine",
                    volcengine_app_id="",
                    volcengine_token="",
                    _env_file=None,
                )

    def test_volcengine_provider_requires_bucket_name(self):
        with TemporaryDirectory() as td:
            module = self._load_config_module(td)
            with self.assertRaises(ValidationError):
                module.Config(
                    telegram_bot_token="123456:abc",
                    transcription_provider="volcengine",
                    volcengine_app_id="app-id",
                    volcengine_token="token-value",
                    volcengine_access_key="ak",
                    volcengine_secret_access_key="sk",
                    volcengine_tos_bucket_name="",
                    volcengine_tos_endpoint="https://tos-cn-shanghai.volces.com",
                    _env_file=None,
                )

    def test_volcengine_provider_requires_secret_access_key(self):
        with TemporaryDirectory() as td:
            module = self._load_config_module(td)
            with self.assertRaises(ValidationError):
                module.Config(
                    telegram_bot_token="123456:abc",
                    transcription_provider="volcengine",
                    volcengine_app_id="app-id",
                    volcengine_token="token-value",
                    volcengine_access_key="ak",
                    volcengine_secret_access_key="",
                    volcengine_tos_bucket_name="voice-stage",
                    volcengine_tos_endpoint="https://tos-cn-shanghai.volces.com",
                    _env_file=None,
                )

    def test_volcengine_provider_requires_tos_endpoint(self):
        with TemporaryDirectory() as td:
            module = self._load_config_module(td)
            with self.assertRaises(ValidationError):
                module.Config(
                    telegram_bot_token="123456:abc",
                    transcription_provider="volcengine",
                    volcengine_app_id="app-id",
                    volcengine_token="token-value",
                    volcengine_access_key="ak",
                    volcengine_secret_access_key="sk",
                    volcengine_tos_bucket_name="voice-stage",
                    volcengine_tos_endpoint="",
                    _env_file=None,
                )

    def test_volcengine_provider_with_new_credentials_is_valid(self):
        with TemporaryDirectory() as td:
            module = self._load_config_module(td)
            cfg = module.Config(
                telegram_bot_token="123456:abc",
                transcription_provider="volcengine",
                volcengine_app_id="app-id",
                volcengine_token="token-value",
                volcengine_access_key="ak",
                volcengine_secret_access_key="sk",
                volcengine_tos_bucket_name="voice-stage",
                volcengine_tos_endpoint="https://tos-cn-shanghai.volces.com",
                _env_file=None,
            )
            self.assertEqual(cfg.transcription_provider, "volcengine")
            self.assertEqual(cfg.volcengine_cluster, "volc_auc_common")

    def test_volcengine_provider_uses_default_cluster_when_blank(self):
        with TemporaryDirectory() as td:
            module = self._load_config_module(td)
            cfg = module.Config(
                telegram_bot_token="123456:abc",
                transcription_provider="volcengine",
                volcengine_app_id="app-id",
                volcengine_token="token-value",
                volcengine_access_key="ak",
                volcengine_secret_access_key="sk",
                volcengine_tos_bucket_name="voice-stage",
                volcengine_tos_endpoint="https://tos-cn-shanghai.volces.com",
                volcengine_cluster="",
                _env_file=None,
            )
            self.assertEqual(cfg.volcengine_cluster, "volc_auc_common")

    def test_voice_reply_defaults_are_loaded(self):
        with TemporaryDirectory() as td:
            module = self._load_config_module(td)
            cfg = module.Config(telegram_bot_token="123456:abc", _env_file=None)
            self.assertEqual(cfg.voice_reply_persona, "Tingting")

    def test_inbound_document_size_limit_defaults_and_validation(self):
        with TemporaryDirectory() as td:
            module = self._load_config_module(td)
            cfg = module.Config(telegram_bot_token="123456:abc", _env_file=None)
            self.assertEqual(cfg.max_document_size_mb, 10)

            with patch.dict(
                os.environ,
                {"CCC_MAX_DOCUMENT_SIZE_MB": "8"},
                clear=False,
            ):
                custom = module.Config(
                    telegram_bot_token="123456:abc",
                    _env_file=None,
                )
            self.assertEqual(custom.max_document_size_mb, 8)

            with self.assertRaises(ValidationError):
                module.Config(
                    telegram_bot_token="123456:abc",
                    CCC_MAX_DOCUMENT_SIZE_MB=0,
                    _env_file=None,
                )
            with self.assertRaises(ValidationError):
                module.Config(
                    telegram_bot_token="123456:abc",
                    CCC_MAX_DOCUMENT_SIZE_MB=21,
                    _env_file=None,
                )

    def test_auto_new_session_hours_defaults_to_24(self):
        with TemporaryDirectory() as td:
            module = self._load_config_module(td)
            cfg = module.Config(telegram_bot_token="123456:abc", _env_file=None)
            self.assertEqual(cfg.auto_new_session_after_hours, 24.0)

    def test_auto_new_session_hours_can_be_disabled(self):
        with TemporaryDirectory() as td:
            module = self._load_config_module(td)
            cfg = module.Config(
                telegram_bot_token="123456:abc",
                auto_new_session_after_hours="off",
                _env_file=None,
            )
            self.assertIsNone(cfg.auto_new_session_after_hours)

    def test_auto_new_session_hours_accepts_custom_number(self):
        with TemporaryDirectory() as td:
            module = self._load_config_module(td)
            cfg = module.Config(
                telegram_bot_token="123456:abc",
                auto_new_session_after_hours="12",
                _env_file=None,
            )
            self.assertEqual(cfg.auto_new_session_after_hours, 12.0)


class AllowedUserIdsConfigTests(unittest.TestCase):
    def _load_with_env(self, project_root: str, **extra_env):
        with patch.dict(
            os.environ,
            {
                "PROJECT_ROOT": project_root,
                "TELEGRAM_BOT_TOKEN": "123456:abc",
                **extra_env,
            },
            clear=True,
        ):
            sys.modules.pop("telegram_bot.utils.config", None)
            return importlib.import_module("telegram_bot.utils.config")

    def test_allowed_user_ids_accepts_comma_separated_env(self):
        with TemporaryDirectory() as td:
            module = self._load_with_env(td, ALLOWED_USER_IDS="123,456,789")
            self.assertEqual(module.config.allowed_user_ids, [123, 456, 789])

    def test_allowed_user_ids_accepts_json_array_env_for_compatibility(self):
        with TemporaryDirectory() as td:
            module = self._load_with_env(td, ALLOWED_USER_IDS="[123, 456, 789]")
            self.assertEqual(module.config.allowed_user_ids, [123, 456, 789])

    def test_allowed_user_ids_ignores_blank_segments(self):
        with TemporaryDirectory() as td:
            module = self._load_with_env(td, ALLOWED_USER_IDS="123, ,456,")
            self.assertEqual(module.config.allowed_user_ids, [123, 456])


class MaxBubbleCharsConfigTests(unittest.TestCase):
    def _load_with_env(self, project_root: str, **extra_env):
        with patch.dict(
            os.environ,
            {
                "PROJECT_ROOT": project_root,
                "TELEGRAM_BOT_TOKEN": "123456:abc",
                **extra_env,
            },
            clear=True,
        ):
            sys.modules.pop("telegram_bot.utils.config", None)
            return importlib.import_module("telegram_bot.utils.config")

    def test_default_is_1200(self):
        with TemporaryDirectory() as td:
            module = self._load_with_env(td)
            self.assertEqual(module.config.telegram_max_bubble_chars, 1200)

    def test_clamped_to_hard_limit(self):
        with TemporaryDirectory() as td:
            module = self._load_with_env(td, CCC_TELEGRAM_MAX_BUBBLE_CHARS="99999")
            self.assertEqual(module.config.telegram_max_bubble_chars, 4000)

    def test_clamped_to_floor(self):
        with TemporaryDirectory() as td:
            module = self._load_with_env(td, CCC_TELEGRAM_MAX_BUBBLE_CHARS="50")
            self.assertEqual(module.config.telegram_max_bubble_chars, 200)

    def test_honors_valid_value(self):
        with TemporaryDirectory() as td:
            module = self._load_with_env(td, CCC_TELEGRAM_MAX_BUBBLE_CHARS="1500")
            self.assertEqual(module.config.telegram_max_bubble_chars, 1500)


class StreamingDefaultConfigTests(unittest.TestCase):
    def _load_with_env(self, project_root: str, **extra_env):
        with patch.dict(
            os.environ,
            {
                "PROJECT_ROOT": project_root,
                "TELEGRAM_BOT_TOKEN": "123456:abc",
                **extra_env,
            },
            clear=True,
        ):
            sys.modules.pop("telegram_bot.utils.config", None)
            return importlib.import_module("telegram_bot.utils.config")

    def test_streaming_off_by_default(self):
        with TemporaryDirectory() as td:
            module = self._load_with_env(td)
            self.assertFalse(module.config.enable_streaming)

    def test_streaming_can_be_enabled(self):
        with TemporaryDirectory() as td:
            module = self._load_with_env(td, CCC_TELEGRAM_STREAMING="true")
            self.assertTrue(module.config.enable_streaming)

    def test_option_buttons_off_by_default(self):
        with TemporaryDirectory() as td:
            module = self._load_with_env(td)
            self.assertFalse(module.config.enable_option_buttons)

    def test_option_buttons_can_be_enabled(self):
        with TemporaryDirectory() as td:
            module = self._load_with_env(td, CCC_TELEGRAM_OPTION_BUTTONS="true")
            self.assertTrue(module.config.enable_option_buttons)


if __name__ == "__main__":
    unittest.main()
