"""A notice row is drawn whether or not a message fills it.

A row that only appears when the engine starts reporting something pushes
everything under it down one line and pulls it back when the report ends: the
card reflows on every redraw, and the reader's eye chases the bar and the
buttons. The binding shape is a row made outside any condition, plus a second
label drawn into the same row for the empty case.

An empty string claims no height, so the empty case draws the reserved slot
instead, which is a label with a line but no words.

Order is the same rule: a message reads as the status it explains, so it sits
under the status line and above the progress bar, not under the bar.

The scan is structural, over `sculptr/ui.py`, because both defects are draw
shapes, not message text.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

UI_SOURCE = Path(__file__).resolve().parents[1] / "sculptr" / "ui.py"

SLOT_NAME = "NOTICE_SLOT"
ROW_NAME = "reason_row"


def _card_units(tree) -> list:
    """The card drawers that carry a transfer status: the Downloads rows and a model card."""
    units = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        texts = {
            inner.value
            for inner in ast.walk(node)
            if isinstance(inner, ast.Constant) and isinstance(inner.value, str)
        }
        if node.name == "_draw_transfer_rows" or (
            node.name == "draw" and "sculptr.model_action" in texts
        ):
            units.append((node.name, node))
    return units


def _conditional_nodes(unit) -> list:
    """Every node that sits inside a condition in this unit: nothing there is unconditional."""
    inside = []
    for node in ast.walk(unit):
        if isinstance(node, ast.If):
            for branch in (*node.body, *node.orelse):
                inside.extend(inner for inner in ast.walk(branch))
    return inside


def _label_lines(unit, receiver) -> list:
    """Lines where `receiver` is labelled."""
    lines = []
    for node in ast.walk(unit):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "label":
            continue
        if isinstance(node.func.value, ast.Name) and node.func.value.id == receiver:
            lines.append(node.lineno)
    return lines


class ReasonSlotTests(unittest.TestCase):
    def test_the_notice_row_exists_whether_or_not_a_message_fills_it(self):
        tree = ast.parse(UI_SOURCE.read_text(encoding="utf-8"), filename=str(UI_SOURCE))
        units = _card_units(tree)
        self.assertGreaterEqual(len(units), 2, msg="only one card drawer carries a transfer status")

        for name, unit in units:
            made = [
                node
                for node in ast.walk(unit)
                if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == ROW_NAME for target in node.targets)
            ]
            self.assertEqual(len(made), 1, msg=f"{name} makes the notice row more than once")
            self.assertFalse(
                any(node is made[0] for node in _conditional_nodes(unit)),
                msg=f"{name} builds the row inside a condition, so an empty frame has no row",
            )
            row_value = made[0].value
            self.assertTrue(
                isinstance(row_value, ast.Call)
                and isinstance(row_value.func, ast.Attribute)
                and row_value.func.attr == "row",
                msg=f"{name} draws the notice into something that is not its own row",
            )

            drawn = _label_lines(unit, ROW_NAME)
            self.assertEqual(len(drawn), 2, msg=f"{name} labels the notice row once: the empty case draws nothing, so the card shrinks")
            self.assertIn(SLOT_NAME, {inner.id for inner in ast.walk(unit) if isinstance(inner, ast.Name)}, msg=f"{name} has no reserved slot to draw when the message is gone")

    def test_the_notice_reads_under_the_status_and_above_the_bar(self):
        tree = ast.parse(UI_SOURCE.read_text(encoding="utf-8"), filename=str(UI_SOURCE))
        for name, unit in _card_units(tree):
            bar = [
                node.lineno
                for node in ast.walk(unit)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "progress"
            ]
            if not bar:
                continue
            drawn = _label_lines(unit, ROW_NAME)
            self.assertLess(
                max(drawn),
                min(bar),
                msg=f"{name} draws the notice below the progress bar, so it does not read with the status it explains",
            )
            self.assertEqual(
                _label_lines(unit, "card"),
                [],
                msg=f"{name} still labels the card box itself, and a label on the box is where the conditional row lived",
            )

    def test_the_reserved_slot_claims_a_line_without_words(self):
        source = UI_SOURCE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(UI_SOURCE))
        made = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == SLOT_NAME for target in node.targets)
        ]
        self.assertEqual(len(made), 1, msg="the reserved slot is defined more than once")
        value = ast.literal_eval(made[0].value)
        self.assertIsInstance(value, str, msg="the reserved slot is not text")
        self.assertNotEqual(value, "", msg="an empty label claims no height, so the row collapses")
        self.assertEqual(value.strip(), "", msg="the reserved slot has visible words")


if __name__ == "__main__":
    unittest.main()
