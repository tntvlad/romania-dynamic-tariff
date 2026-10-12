"""Test bootstrap: import the pure component modules without ``__init__.py``.

``__init__.py`` imports ``homeassistant``, which is not installed in this
environment. We register lightweight package stubs in ``sys.modules`` so only
``core.py`` and ``database.py`` (both dependency-free) get imported.
"""
from __future__ import annotations

import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_COMPONENT = "custom_components.romania_dynamic_tariff"


def _stub_package(name: str, path: str) -> None:
    mod = types.ModuleType(name)
    mod.__path__ = [path]
    mod.__package__ = name
    sys.modules[name] = mod


def bootstrap() -> None:
    """Register the package stubs once."""
    if _COMPONENT in sys.modules:
        return
    _stub_package(
        "custom_components", os.path.join(_ROOT, "custom_components")
    )
    _stub_package(_COMPONENT, os.path.join(_ROOT, *_COMPONENT.split(".")))
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)