"""The suite must exercise this checkout, not a bridge deployed on the host.

Every test here imports `telegram_bot`, which is this directory under its
package name. Nothing inside the checkout answers to that name, so the import
resolves from wherever sys.path happens to offer it — and on a node that runs
the bridge, that is the deployed tree at /opt/ccc-node. The suite then reports
on code nobody is editing.

Both directions of that failure have been observed. A deployment older than the
checkout produced three red cases on a clean `main` (a Literal that the checkout
had already widened), which reads as "my change broke something" and costs a
bisect. The quiet direction is worse: when the deployment merely differs, the
run is green for code that never executed, and a local "tests pass" says nothing
about the diff under review.

`bridge/pyproject.toml` pins `pythonpath` to the checked-in `.github/pythonpath`
shim so this cannot depend on remembering an env var. This test is the tripwire
for that pin: if the option is dropped or the shim moves, the suite says so here
instead of silently grading the wrong tree.
"""

from __future__ import annotations

from pathlib import Path
import tomllib

import telegram_bot


def test_telegram_bot_resolves_to_this_checkout() -> None:
    checkout = Path(__file__).resolve().parents[1]
    # The shim is a symlink, so resolve() lands on the real directory rather
    # than on .github/pythonpath/telegram_bot — comparing unresolved paths
    # would fail on a correctly configured run.
    imported = Path(telegram_bot.__file__).resolve().parent
    assert imported == checkout, (
        f"tests are importing telegram_bot from {imported}, not from the "
        f"checkout at {checkout}. A bare `pytest` picks up a deployed bridge "
        f"(e.g. /opt/ccc-node) when bridge/pyproject.toml's "
        f"[tool.pytest.ini_options] pythonpath entry is missing."
    )


def test_pythonpath_shim_is_present_and_points_here() -> None:
    checkout = Path(__file__).resolve().parents[1]
    shim = checkout.parent / ".github" / "pythonpath" / "telegram_bot"
    assert shim.is_symlink(), f"missing the packaging shim at {shim}"
    assert shim.resolve() == checkout, (
        f"{shim} resolves to {shim.resolve()}, expected {checkout}"
    )


def test_pyproject_pins_the_shim_on_pythonpath() -> None:
    # The resolution test above cannot fail in CI even if the pin is deleted,
    # because the workflow exports PYTHONPATH itself — so on its own it would
    # let the pin be removed and only bite whoever next ran the suite locally.
    # Asserting the configuration, not just its effect, is what makes CI able
    # to gate the pin.
    checkout = Path(__file__).resolve().parents[1]
    with (checkout / "pyproject.toml").open("rb") as fh:
        pyproject = tomllib.load(fh)
    entries = pyproject.get("tool", {}).get("pytest", {}).get("ini_options", {}).get(
        "pythonpath", []
    )
    resolved = {(checkout / Path(entry)).resolve() for entry in entries}
    assert (checkout.parent / ".github" / "pythonpath").resolve() in resolved, (
        "bridge/pyproject.toml no longer pins .github/pythonpath on "
        f"[tool.pytest.ini_options] pythonpath (found {entries}). Without it a "
        "bare local `pytest` imports whatever telegram_bot sys.path offers — on "
        "a bridge-running node, the deployed tree instead of this checkout."
    )
