"""A definition is *shown* as the server printed it, and still *compared* canonically.

The canonical form of a definition is its tokens joined by single spaces, so it never holds a
newline. Showing it was the defect: a 400-line view could only ever render one removed and one
added line. ``tests/integration/test_rendered_diff.py`` is the assertion that proves the whole
path; these pin the engine's half of it without a database.
"""

from db_schema_comparer.diff.attributes import SPECS
from db_schema_comparer.diff.engine import diff_inventories
from db_schema_comparer.model.keys import ObjectKey
from db_schema_comparer.model.kinds import ObjectKind
from db_schema_comparer.model.objects import RawValues, View
from db_schema_comparer.normalize.routines import canonical_body
from tests.support.builders import col, inventory, table

PRINTED = " SELECT i.id,\n    i.total\n   FROM invoice i\n  WHERE i.open;"
CHANGED = " SELECT i.id,\n    i.total\n   FROM invoice i\n  WHERE i.paid;"


def view(definition: str | None, *, raw: str | None = None):
    key = ObjectKey(ObjectKind.VIEW, "public", "v")
    return {
        key: View(
            key=key,
            definition=canonical_body(definition),
            raw=RawValues({} if raw is None else {"definition": raw}),
        )
    }


def keep():
    return table("public", "keep", cols=[col("k")])


def only_delta(master_objects, target_objects):
    result = diff_inventories(
        inventory(keep(), master_objects, label="prod"),
        inventory(keep(), target_objects, label="qa"),
    )
    (finding,) = result.findings
    (delta,) = [d for d in finding.deltas if d.attribute == "view.definition"]
    return delta


def test_every_body_spec_can_show_text():
    # The flag and the display getter must not drift apart: a body spec without one renders a
    # diff of its canonical value, which is the defect this file exists to keep fixed.
    missing = [
        spec.name
        for specs in SPECS.values()
        for spec in specs
        if spec.body and spec.display_getter is None
    ]
    assert missing == []


def test_the_display_text_is_the_raw_definition_with_its_line_breaks():
    delta = only_delta(view(PRINTED, raw=PRINTED), view(CHANGED, raw=CHANGED))
    assert delta.master_display == PRINTED
    assert delta.target_display == CHANGED
    assert "\n" in delta.master_display


def test_the_compared_values_stay_canonical():
    delta = only_delta(view(PRINTED, raw=PRINTED), view(CHANGED, raw=CHANGED))
    assert delta.master_value == canonical_body(PRINTED)
    assert "\n" not in delta.master_value
    assert "\n" not in delta.target_value


def test_an_inventory_without_raw_text_falls_back_to_the_canonical_value():
    # A capture from before raw definitions were recorded. It still shows something.
    delta = only_delta(view(PRINTED), view(CHANGED))
    assert delta.master_display == canonical_body(PRINTED)
    assert delta.target_display == canonical_body(CHANGED)


def test_one_side_without_raw_text_falls_back_on_that_side_only():
    delta = only_delta(view(PRINTED), view(CHANGED, raw=CHANGED))
    assert delta.master_display == canonical_body(PRINTED)
    assert delta.target_display == CHANGED


def test_a_layout_only_difference_in_the_raw_text_is_still_no_finding():
    # The guarantee that matters: equality is decided on the canonical value, so reflowing a view
    # produces no finding however differently the server printed it.
    reflowed = PRINTED.replace("\n", "\n\n").replace("    ", "\t")
    result = diff_inventories(
        inventory(keep(), view(PRINTED, raw=PRINTED), label="prod"),
        inventory(keep(), view(reflowed, raw=reflowed), label="qa"),
    )
    assert result.findings == ()
