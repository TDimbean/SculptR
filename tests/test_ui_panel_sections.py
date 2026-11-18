"""Section structure for the Models tab of the viewport panel.

Invariance under test: the license notice is a section of its own, a sibling of
"Folder" and "Models" under `SCULPTR_PT_root`, so Blender draws it as its own
collapsible section instead of as loose rows inside the model list. The
ordering rule Blender applies is `bl_parent_id` plus `bl_order` plus `poll()`,
so the harness stubs only those three shapes and checks what the sidebar would
collect. A duplicated `bl_order` inside one tab is a real Blender ambiguity,
and a license notice that survives in the model section is a duplicate notice.
"""

from __future__ import annotations

import importlib
import inspect
import sys
import tempfile
import types
import unittest
from pathlib import Path

# The stub bases and property factories `sculptr.ui` touches while its module
# body runs; see tests/test_extension_imports.py for the same, wider harness.
BTYPE_BASES = ("AddonPreferences", "FileHandler", "Operator", "Panel", "PropertyGroup")
PROPERTY_FACTORIES = (
    "BoolProperty",
    "EnumProperty",
    "IntProperty",
    "PointerProperty",
    "StringProperty",
)


def _install_bpy_stub():
    """Put the minimum believable `bpy` in place and return the module names."""

    def _noop(*_args, **_kwargs):
        return None

    bpy = types.ModuleType("bpy")
    bpy_types = types.ModuleType("bpy.types")
    bpy_props = types.ModuleType("bpy.props")
    for name in BTYPE_BASES:
        setattr(bpy_types, name, type(name, (_NoopBase,), {}))
    for name in PROPERTY_FACTORIES:
        setattr(bpy_props, name, _noop)
    bpy.types = bpy_types
    bpy.props = bpy_props
    bpy_utils = types.ModuleType("bpy.utils")

    def _user_resource(kind, *, path=""):
        # properties.py resolves a Blender path as a property default at import
        # time; the value only has to exist for the property factory to take it.
        return str(Path(tempfile.gettempdir()) / "sculptr" / (path or kind))

    bpy_utils.user_resource = _user_resource
    bpy.utils = bpy_utils

    stubs = {"bpy": bpy, "bpy.types": bpy_types, "bpy.props": bpy_props, "bpy.utils": bpy.utils}
    sys.modules.update(stubs)
    return list(stubs)


class _NoopBase:
    def __getattr__(self, _name):
        return None


def _settings_context(active_tab):
    """A context object shaped like the one the panel poll()s read."""

    settings = types.SimpleNamespace(active_tab=active_tab)
    return types.SimpleNamespace(scene=types.SimpleNamespace(sculptr=settings))


class PanelSectionTests(unittest.TestCase):
    def test_license_is_its_own_section(self):
        installed = _install_bpy_stub()
        try:
            ui = importlib.import_module("sculptr.ui")
            children = [cls for cls in ui.CLASSES if getattr(cls, "bl_parent_id", None) == "SCULPTR_PT_root"]
            by_idname = {cls.bl_idname: cls for cls in children}

            self.assertIn("SCULPTR_PT_license", by_idname, msg="license panel missing")
            self.assertEqual(by_idname["SCULPTR_PT_license"].bl_label, "License")

            for tab in ("GENERATE", "MODELS"):
                ctx = _settings_context(tab)
                visible = sorted(
                    (cls for cls in children if cls.poll(ctx)), key=lambda cls: cls.bl_order
                )
                orders = [cls.bl_order for cls in visible]
                self.assertEqual(
                    len(orders),
                    len(set(orders)),
                    msg=f"{tab} tab has a duplicated bl_order: {orders}",
                )
                if tab != "MODELS":
                    continue
                self.assertEqual(
                    [cls.bl_label for cls in visible],
                    ["Folder", "Models", "License"],
                    msg=f"MODELS sections are {[cls.bl_label for cls in visible]!r}",
                )

            models = by_idname["SCULPTR_PT_models"]
            self.assertNotIn(
                "HUNYUAN_LICENSE_URL",
                inspect.getsource(models.draw),
                msg="the license notice is also drawn inside the Models section",
            )
            license = by_idname["SCULPTR_PT_license"]
            self.assertIn(
                "HUNYUAN_LICENSE_URL",
                inspect.getsource(license.draw),
                msg="the license section lost the license URLs",
            )
        finally:
            for name in installed:
                sys.modules.pop(name, None)


if __name__ == "__main__":
    unittest.main()
