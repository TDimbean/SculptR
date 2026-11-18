"""One card owns the transfer lifecycle: the model card, not the download rows.

The same download was offered twice before -- Resume/Pause/Discard on the
transfer row under the Folder tab *and* Cancel/Pause/Resume on the model card
under Models. Two owners for one action means two states that can disagree, and
the reader has to hunt for the button that matches the card they are looking at.
So the binding shape is: the Downloads section only reports, and every lifecycle
operator for a transfer is pressed from that model's card.

The scan is structural over `sculptr/ui.py`, because the rule is about where a
control is drawn, not about how it behaves. The two-step discard is pinned too:
deleting committed gigabytes still needs a second click, and a control that lost
that pair is a different, worse control.

`ui_helpers.transfer_resumable` fed only the Resume button that used to sit on
the transfer row, so its name must not survive either.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "sculptr"
UI_SOURCE = ROOT / "ui.py"
HELPERS_SOURCE = ROOT / "ui_helpers.py"

# Every action a transfer can be pressed through. All three must be reachable
# from the model card, and from nowhere else in the panel.
LIFECYCLE_OPERATORS = (
    "sculptr.resume_transfer",
    "sculptr.pause_transfer",
    "sculptr.discard_transfer",
)

# The card must still be able to delete a transfer, and delete it in two steps.
CONFIRM_PAIR_TEXTS = ("Confirm Discard", "Keep", "Discard")
# A transfer whose bytes are already installed or already discarded has nothing
# left for Discard to remove.
DISCARD_EXCLUDED_STATES = ("installed", "discarded")


def _named_draw_units(tree):
    """Every function and method in the module, by simple name."""
    units = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            units.setdefault(node.name, []).append(node)
    return units


def _operator_targets(node):
    """The operator names an expression tree presses, via `.operator("...")`."""
    names = []
    for inner in ast.walk(node):
        if not (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)):
            continue
        if inner.func.attr != "operator" or not inner.args:
            continue
        first = inner.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            names.append(first.value)
    return names


def _string_constants(node):
    values = []
    for inner in ast.walk(node):
        if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
            values.append(inner.value)
    return values


class TransferControlTests(unittest.TestCase):
    def test_downloads_section_offers_no_controls(self):
        source = UI_SOURCE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(UI_SOURCE))
        rows = _named_draw_units(tree).get("_draw_transfer_rows", [])
        self.assertEqual(len(rows), 1, msg="the download rows moved out of the panel")
        pressed = _operator_targets(rows[0])
        self.assertEqual(pressed, [], msg=f"the download rows still press controls: {pressed}")
        # The section stays: it reports engine state the model card does not carry.
        self.assertIn('text=f"Downloads ({len(records)})"', source, msg="the read-only report vanished")

    def test_only_the_model_card_presses_the_lifecycle(self):
        source = UI_SOURCE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(UI_SOURCE))
        units = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        card_units = [
            node for node in units if "sculptr.model_action" in _operator_targets(node)
        ]
        self.assertEqual(len(card_units), 1, msg="more than one unit owns model actions")
        card = card_units[0]
        pressed = _operator_targets(card)
        for name in LIFECYCLE_OPERATORS:
            self.assertIn(name, pressed, msg=f"{name} is no longer on the model card")
        for node in units:
            if node is card:
                continue
            strays = [name for name in _operator_targets(node) if name in LIFECYCLE_OPERATORS]
            self.assertEqual(
                strays,
                [],
                msg=f"{node.name} presses {strays} outside the model card",
            )

    def test_the_card_still_deletes_in_two_steps(self):
        source = UI_SOURCE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(UI_SOURCE))
        card = next(
            node
            for name, nodes in _named_draw_units(tree).items()
            for node in nodes
            if name == "draw" and "sculptr.model_action" in _operator_targets(node)
        )
        texts = _string_constants(card)
        for text in CONFIRM_PAIR_TEXTS:
            self.assertIn(text, texts, msg=f"the discard flow lost {text}")
        self.assertTrue(
            any(
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "discard_armed"
                for inner in ast.walk(card)
            ),
            msg="the pending discard is no longer read back from the card",
        )
        for state in DISCARD_EXCLUDED_STATES:
            self.assertIn(state, texts, msg=f"Discard is offered for a {state} transfer again")

    def test_the_helper_that_fed_the_removed_button_is_gone(self):
        for path in (UI_SOURCE, HELPERS_SOURCE):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("transfer_resumable", text, msg=f"{path} keeps a helper nothing calls")


if __name__ == "__main__":
    unittest.main()
