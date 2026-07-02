"""CI dependency-install hardening checks for OpenSSF Scorecard."""

from pathlib import Path
import os
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
HASH_LOCK = REPO_ROOT / ".github" / "requirements" / "bridge-ci.txt"
PYTHONPATH_SHIM = REPO_ROOT / ".github" / "pythonpath" / "telegram_bot"


def test_ci_pip_installs_use_hash_locked_requirements():
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    pip_lines = [line.strip() for line in workflow.splitlines() if "pip install" in line]
    assert pip_lines, "expected CI workflow to install Python dependencies"
    assert all("--require-hashes" in line for line in pip_lines), pip_lines
    assert all(".github/requirements/bridge-ci.txt" in line for line in pip_lines), pip_lines


def test_ci_hash_lock_contains_hashes_for_tooling_and_bridge_dependencies():
    lock_text = HASH_LOCK.read_text(encoding="utf-8")
    for package in ["ruff", "mypy", "pytest", "pytest-cov", "pydantic", "python-dotenv"]:
        assert f"{package}==" in lock_text
    assert "--hash=sha256:" in lock_text


def test_ci_pythonpath_shim_exposes_bridge_package_without_editable_install():
    assert PYTHONPATH_SHIM.is_symlink()
    assert PYTHONPATH_SHIM.resolve() == REPO_ROOT / "bridge"


def test_ci_pythonpath_shim_imports_telegram_bot_without_editable_install():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PYTHONPATH_SHIM.parent)
    env.setdefault("PROJECT_ROOT", str(REPO_ROOT / "bridge"))
    env.setdefault("TELEGRAM_BOT_TOKEN", "123456:test")
    subprocess.run(
        [sys.executable, "-c", "import telegram_bot, telegram_bot.core.bot"],
        cwd=REPO_ROOT,
        env=env,
        check=True,
    )
