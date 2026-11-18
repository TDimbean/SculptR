"""One message draws one label with one icon.

A message that gets split before it is drawn turns into a row per fragment, each
carrying its own icon: the reader sees three bubbles for one sentence, or two
stacked rows, instead of the single wide row a Blender label grows to. So the
binding shape is a loop that labels with its own loop variable -- whatever made
that variable (a splitter, a join, a tuple literal).

The scan is structural, over `sculptr/ui.py` itself, because the defect is the
draw shape, not the message text. The hand splitter `_wrap_text` is gone with the
loops it fed, so its name must not survive either.

Length belongs to the same rule: a label never widens its panel, and Blender
replaces the middle of a label that overflows the drawer with "...", which reads
as the same split message. So the messages that must stay visible are pinned to
the width one row can hold.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

UI_SOURCE = Path(__file__).resolve().parents[1] / "sculptr" / "ui.py"


def _label_uses_loop_variable(node, target) -> bool:
    """True for a `.label(...)` call in this loop that draws the loop variable."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return False
    if node.func.attr != "label":
        return False
    drawn = [node.value] if node.args else [kw.value for kw in node.keywords if kw.arg == "text"]
    return any(isinstance(value, ast.Name) and value.id == target for value in drawn)

# Messages that must still be drawn, so deleting the splitter cannot silence them.
REQUIRED_MESSAGE_TEXT = (
    "Download running. Pause or cancel it before changing the folder.",
)

# A label never widens its panel: Blender keeps the row at the drawer width and
# replaces the middle of an overflowing label with "...". Measured in the Folder
# panel, one row fits ~70 glyphs, so a message has to stay inside that.
MAX_LABEL_CHARS = 70


class NoticeRowTests(unittest.TestCase):
    def test_no_message_is_split_before_it_is_drawn(self):
        source = UI_SOURCE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(UI_SOURCE))

        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.For) or not isinstance(node.target, ast.Name):
                continue
            target = node.target.id
            for inner in ast.walk(node):
                if _label_uses_loop_variable(inner, target):
                    offenders.append(f"line {inner.lineno}: labels {target} inside a loop")
        self.assertEqual(offenders, [], msg=f"messages split into several labels: {offenders}")

        self.assertNotIn("_wrap_text", source, msg="the splitter helper survived its loops")
        for text in REQUIRED_MESSAGE_TEXT:
            self.assertIn(text, source, msg="a wrapped notice was deleted, not unified")
            self.assertLessEqual(
                len(text),
                MAX_LABEL_CHARS,
                msg=f"a one-row message longer than {MAX_LABEL_CHARS} chars gets elided by Blender",
            )


if __name__ == "__main__":
    unittest.main()
