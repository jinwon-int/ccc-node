"""Build targeting follows Termux Python, independently of the device OS SDK."""
import io
from types import SimpleNamespace

import pytest

from telegram_bot import dependency_bootstrap as bootstrap


@pytest.fixture
def android(monkeypatch):
    monkeypatch.setattr(bootstrap.sys, "getandroidapilevel", lambda: 24, raising=False)
    monkeypatch.setattr(bootstrap.sysconfig, "get_platform", lambda: "android-24-arm64_v8a")
    monkeypatch.setattr(bootstrap.platform, "android_ver", lambda: SimpleNamespace(api_level=24), raising=False)
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda *a, **kw: pytest.fail("must not query device SDK"))


def test_build_target_is_python_api_not_device_sdk(android):
    env = {"TERMUX_VERSION": "fixture"}
    bootstrap.ensure_android_api_level(env, stdout=io.StringIO())
    assert env["ANDROID_API_LEVEL"] == "24"


def test_matching_override_is_preserved(android):
    env = {"PREFIX": "/data/data/com.termux/files/usr", "ANDROID_API_LEVEL": "24"}
    bootstrap.ensure_android_api_level(env, stdout=io.StringIO())
    assert env["ANDROID_API_LEVEL"] == "24"


@pytest.mark.parametrize("value", ["33", "36", "23", "024", "24junk", " 24", "$(secret)"])
def test_mismatched_or_invalid_override_is_rejected_without_echo(android, value):
    env = {"TERMUX_VERSION": "fixture", "ANDROID_API_LEVEL": value}
    with pytest.raises(ValueError, match="must match Python build API 24") as exc:
        bootstrap.ensure_android_api_level(env, stdout=io.StringIO())
    assert str(exc.value) == "ANDROID_API_LEVEL must match Python build API 24; remove the override"
    assert env["ANDROID_API_LEVEL"] == value


def test_newer_runtime_packaging_api_still_builds_for_python_minimum(android, monkeypatch):
    monkeypatch.setattr(bootstrap.platform, "android_ver", lambda: SimpleNamespace(api_level=36))
    assert bootstrap.android_build_api() == 24


@pytest.mark.parametrize("target", ["linux-aarch64", "android-33-arm64_v8a", "android-invalid-arm64_v8a"])
def test_inconsistent_sysconfig_is_rejected(android, monkeypatch, target):
    monkeypatch.setattr(bootstrap.sysconfig, "get_platform", lambda: target)
    with pytest.raises(ValueError, match="unavailable or inconsistent"):
        bootstrap.android_build_api()


@pytest.mark.parametrize("api", [None, True, 0, "24", 1001])
def test_missing_or_invalid_build_api_is_rejected(android, monkeypatch, api):
    monkeypatch.setattr(bootstrap.sys, "getandroidapilevel", lambda: api)
    with pytest.raises(ValueError):
        bootstrap.android_build_api()


def test_packaging_api_cannot_be_lower_than_build_api(android, monkeypatch):
    monkeypatch.setattr(bootstrap.platform, "android_ver", lambda: SimpleNamespace(api_level=23))
    with pytest.raises(ValueError):
        bootstrap.android_build_api()


def test_non_termux_environment_is_unchanged(monkeypatch):
    monkeypatch.setattr(bootstrap, "android_build_api", lambda: pytest.fail("unexpected probe"))
    env = {"ANDROID_API_LEVEL": "33"}
    bootstrap.ensure_android_api_level(env)
    assert env == {"ANDROID_API_LEVEL": "33"}
