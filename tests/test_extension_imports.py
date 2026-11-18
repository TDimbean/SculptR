"""Enable-time import coverage for the Blender extension.

Invariant under test: enabling the extension must import every module of the
package without raising. Blender swallows an import failure into a one-line
report ("name 'Any' is not defined" was such a report), and a module body runs
at import time -- including the evaluation of parameter annotations -- so an
unbound typing name kills the whole extension silently. This harness runs that
same path without Blender: `bpy` is stubbed with the few module-level shapes the
extension touches (base classes, property factories), every module is imported,
and any failure is named with the module that broke.
"""

from __future__ import annotations

import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path

# Every module of the extension, in dependency order. `sculptr.__init__` is
# included because Blender imports the package itself when the extension is
# enabled.
MODULE_NAMES = (
    "sculptr",
    "sculptr.catalog",
    "sculptr.diagnostics",
    "sculptr.process",
    "sculptr.properties",
    "sculptr.bridge",
    "sculptr.transfers",
    "sculptr.ui_helpers",
    "sculptr.operators",
    "sculptr.ui",
    "sculptr.engine",
    "sculptr.engine.protocol",
    "sculptr.engine.system",
    "sculptr.engine.hub",
    "sculptr.engine.daemon",
    "sculptr.engine.inference_runner",
)

# The only bpy attributes the extension touches while its modules are being
# imported: the base classes listed in `class ... (bpy.types.X)`, the property
# factories called in class bodies, and the resource lookup that the settings
# class resolves as a default value. Everything else on bpy is used inside
# functions, which this harness does not run.
BTYPE_BASES = ("AddonPreferences", "FileHandler", "Operator", "Panel", "PropertyGroup")
PROPERTY_FACTORIES = (
    "BoolProperty",
    "EnumProperty",
    "IntProperty",
    "PointerProperty",
    "StringProperty",
)
UTILS_CALLABLES = ("user_resource",)


def _install_bpy_stub():
    """Put the minimum believable `bpy` in place and return the module names."""

    def _noop(*_args, **_kwargs):
        return None

    def _user_resource(kind, *, path=""):
        # A real Blender path would be fine here; the settings class only needs
        # a value to hand to a property factory, and nothing else reads it.
        return str(Path(tempfile.gettempdir()) / "sculptr" / (path or kind))

    class _Node:
        def __getattr__(self, _name):
            return _noop

    bpy = types.ModuleType("bpy")
    bpy_types = types.ModuleType("bpy.types")
    bpy_props = types.ModuleType("bpy.props")
    bpy_utils = types.ModuleType("bpy.utils")
    for name in BTYPE_BASES:
        setattr(bpy_types, name, type(name, (_Node,), {}))
    for name in PROPERTY_FACTORIES:
        setattr(bpy_props, name, _noop)
    for name in UTILS_CALLABLES:
        setattr(bpy_utils, name, _noop if name != "user_resource" else _user_resource)
    bpy.types = bpy_types
    bpy.props = bpy_props
    bpy.utils = bpy_utils

    stubs = {
        "bpy": bpy,
        "bpy.types": bpy_types,
        "bpy.props": bpy_props,
        "bpy.utils": bpy_utils,
    }
    sys.modules.update(stubs)
    return list(stubs)


class ImportTests(unittest.TestCase):
    def test_extension_imports_cleanly(self):
        installed = _install_bpy_stub()
        try:
            failures = []
            for name in MODULE_NAMES:
                try:
                    importlib.import_module(name)
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"{name}: {type(exc).__name__}: {exc}")
        finally:
            # The stub must not leak: an extension module that needs real Blender
            # would otherwise see a fake bpy in a later test.
            for name in installed:
                sys.modules.pop(name, None)
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
