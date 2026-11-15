"""SculptR automated tests.

Run from the repository root:

    python -m unittest discover

The suite is stdlib-only and never touches the network: HTTP is exercised
through scripted fakes, and the engine is exercised in-process or through a fake
process handle. No test imports `bpy`; `tests/support.py` registers the
`SculptR/` directory under the distribution name `sculptr` so the add-on
modules can be imported outside Blender.
"""
