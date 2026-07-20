"""Containment for import-time ``sys.modules`` fakes installed by test modules.

Several test modules replace real modules (``telegram_bot.*``,
``claude_agent_sdk*``) in ``sys.modules`` at import (collection) time so they
can import product modules against light stubs. pytest imports every selected
test module during collection before any test runs, so without containment
those fakes — and any real module transitively imported while they were
active — leak into every other module's test run, producing
collection-order-dependent failures.

Usage, inside an offender module::

    from sys_modules_isolation import ModuleFakesGuard

    _sys_modules_guard = ModuleFakesGuard(__name__).begin()
    ...install fakes / pop + reimport product modules...
    _sys_modules_guard.finish()

``begin()`` snapshots the guarded namespaces. ``finish()`` diffs the current
``sys.modules`` against that snapshot, reverts every change immediately (so
collection of later modules — and every other module's tests — see pristine
state), and registers the diff so the autouse module-scoped fixture in
``conftest.py`` reinstalls the module's fakes only while that module's own
tests run and reverts them again at module teardown.

When the offender runs standalone (``python tests/test_x.py``,
``__name__ == "__main__"``) there is no pytest fixture to reinstall the fakes,
so ``finish()`` leaves them in place — the process is dedicated to that one
module, exactly as before.

``preserved_sys_modules`` is the same snapshot/revert as a context manager for
run-time (in-test) use.
"""

import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from types import ModuleType

_ABSENT = object()

GUARDED_PREFIXES: tuple[str, ...] = ("telegram_bot", "claude_agent_sdk")

# owner module name -> {sys.modules key: (module during owner's tests, pristine original)}
_REGISTRY: dict[str, dict[str, tuple[object, object]]] = {}


def _apply(mapping: Mapping[str, object]) -> None:
    for name, module in mapping.items():
        if module is _ABSENT:
            sys.modules.pop(name, None)
        else:
            assert isinstance(module, ModuleType)
            sys.modules[name] = module


def _snapshot(prefixes: Sequence[str]) -> dict[str, ModuleType]:
    tops = tuple(prefixes)
    return {
        name: module
        for name, module in sys.modules.items()
        if name.split(".", 1)[0] in tops
    }


class ModuleFakesGuard:
    """Snapshot / diff / revert / register import-time sys.modules pollution."""

    def __init__(self, owner: str, prefixes: Sequence[str] = GUARDED_PREFIXES) -> None:
        self._owner = owner
        self._prefixes = tuple(prefixes)
        self._pristine: dict[str, ModuleType] | None = None

    def begin(self) -> "ModuleFakesGuard":
        self._pristine = _snapshot(self._prefixes)
        return self

    def finish(self) -> "ModuleFakesGuard":
        assert self._pristine is not None, "ModuleFakesGuard.begin() must run first"
        changed: dict[str, tuple[object, object]] = {}
        for name, module in _snapshot(self._prefixes).items():
            original = self._pristine.get(name, _ABSENT)
            if original is not module:
                changed[name] = (module, original)
        for name, original in self._pristine.items():
            if name not in sys.modules:
                changed[name] = (_ABSENT, original)
        _REGISTRY[self._owner] = changed
        if self._owner != "__main__":
            _apply({name: original for name, (_module, original) in changed.items()})
        return self


def activate(owner: str) -> Mapping[str, object] | None:
    """Install ``owner``'s registered fakes; return an undo mapping (or None)."""
    changed = _REGISTRY.get(owner)
    if not changed:
        return None
    undo = {name: sys.modules.get(name, _ABSENT) for name in changed}
    _apply({name: module for name, (module, _original) in changed.items()})
    return undo


def deactivate(undo: Mapping[str, object] | None) -> None:
    """Revert an ``activate`` call using the undo mapping it returned."""
    if undo:
        _apply(undo)


@contextmanager
def preserved_sys_modules(prefix: str | Sequence[str] = GUARDED_PREFIXES) -> Iterator[None]:
    """Revert any guarded ``sys.modules`` mutation made inside the block."""
    prefixes = (prefix,) if isinstance(prefix, str) else tuple(prefix)
    snapshot = _snapshot(prefixes)
    try:
        yield
    finally:
        for name in list(sys.modules):
            if name.split(".", 1)[0] in prefixes and name not in snapshot:
                del sys.modules[name]
        sys.modules.update(snapshot)
