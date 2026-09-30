"""Grouping the vendor's shapes, and turning a person's labels into a template set (#756 phase B).

Verification for: `scripts/glyph_inventory.py`.

The two that matter most: a suggestion the person did not confirm makes no template, and the set's
hash changes when any one label does. Everything else is the tool refusing to build from labels it
cannot trust.

Every fixture is authored geometry. No client drawing is read here.
"""

from __future__ import annotations

import csv
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from extraction.glyph_shapes import ShapeSettings, describe
from scripts.glyph_inventory import (
    ALPHABET,
    NOT_A_CHARACTER,
    SIDEWAYS,
    InventoryError,
    main,
    read_reader_settings,
    reference_shapes,
    suggest,
)
from tests.extraction.test_annotations import _appearance, _pdf, _stamp

#: A long dimension line, and the same label drawn twice above it: a `1`, a `0`, an inch mark.
LABELS_APPEARANCE = b"0.2 w 100 500 m 300 500 l S\n" + b"".join(
    f"{110 + dx} 503 m {110 + dx} 508.5 l S\n".encode()  # 1
    + f"{112 + dx} 503 m {115.6 + dx} 503 l {115.6 + dx} 508.5 l {112 + dx} 508.5 l h S\n".encode()
    + f"{117.0 + dx} 506.9 m {117.5 + dx} 508.5 l {118.6 + dx} 506.9 m {119.1 + dx} 508.5 l S\n".encode()
    for dx in (0, 60)
)


READER = """
GV_READER_LINE_MINIMUM_PT=6 \\
GV_READER_GLYPH_MAXIMUM_PT=6 \\
GV_READER_GLYPH_GAP_PT=4 \\
GV_READER_PROXIMITY_LIMIT=0.2 \\
GV_READER_LOCALIZED_MINIMUM_PATHS=1 \\
GV_READER_LOCALIZED_MAXIMUM_SPAN=0.5 \\
GV_READER_FRACTION_BAR_THICKNESS_MAX_PT=0.3 \\
GV_READER_FRACTION_BAR_LENGTH_MIN_PT=1 \\
GV_READER_FRACTION_REACH_PT=3 \\
GV_READER_FRACTION_GLYPH_MIN_PT=1 \\
GV_READER_FRACTION_GLYPH_MAX_PT=12 \\
GV_READER_FRACTION_PROPORTION_MAX=2.5 \\
"""

SHAPE_ARGUMENTS = [
    "--size-px",
    "24",
    "--bezier-steps",
    "8",
    "--stroke-px",
    "1",
    "--dilate-px",
    "1",
    "--minimum-overlap",
    "0.5",
    "--maximum-size-ratio",
    "1.3",
    "--suggest-max-distance",
    "0.8",
    "--suggest-margin",
    "0.15",
    "--suggest-size-ratio",
    "10",
]


@pytest.fixture(scope="module")
def _inventory_once(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """An inventory of the fixture drawing, written by the real command, once for the module."""
    tmp_path = tmp_path_factory.mktemp("inventory")
    drawing = tmp_path / "shop.pdf"
    drawing.write_bytes(
        _pdf(
            annotations=[_stamp(appearance_object=6)],
            extra_objects=[_appearance(LABELS_APPEARANCE)],
        )
    )
    settings = tmp_path / "demo.sh"
    settings.write_text(READER, encoding="utf-8")
    out = tmp_path / "inventory"
    assert (
        main(
            [
                "inventory",
                str(drawing),
                "--pages",
                "1",
                "--out",
                str(out),
                "--reader-settings",
                str(settings),
                *SHAPE_ARGUMENTS,
            ]
        )
        == 0
    )
    return out


@pytest.fixture()
def made(_inventory_once: Path, tmp_path: Path) -> Path:
    """A private copy per test, because a test writes its own `labels.csv` into it."""
    import shutil

    copy = tmp_path / "inventory"
    shutil.copytree(_inventory_once, copy)
    return copy


def _clusters(out: Path) -> list[dict[str, str]]:
    with (out / "clusters.csv").open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def _save_labels(out: Path, labels: dict[str, str], mixed: frozenset[str] = frozenset()) -> None:
    with (out / "labels.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["cluster_id", "label", "mixed", "note"])
        for row in _clusters(out):
            cluster = row["cluster_id"]
            writer.writerow(
                [cluster, labels.get(cluster, ""), "yes" if cluster in mixed else "", ""]
            )


def _build(out: Path, templates: Path, *extra: str) -> int:
    return main(
        [
            "build",
            str(out),
            "--labelled-by",
            "a reviewer",
            "--on",
            "2026-10-01",
            "--templates-root",
            str(templates),
            *extra,
        ]
    )


def _all_labelled(out: Path) -> dict[str, str]:
    """Label every cluster: its suggestion where there is one, `not_a_character` otherwise."""
    return {row["cluster_id"]: row["suggested"] or NOT_A_CHARACTER for row in _clusters(out)}


# ---------------------------------------------------------------------------
# inventory
# ---------------------------------------------------------------------------


def test_the_inventory_groups_the_repeated_label_and_writes_what_a_person_needs(made: Path) -> None:
    """Outcome: two copies of each character make one group of two; the page and guide exist."""
    rows = _clusters(made)

    assert sorted(int(row["count"]) for row in rows) == [2, 2, 2]
    assert all(row["label"] == "" for row in rows), "the tool never fills in a label"
    assert (made / "label.html").exists() and (made / "HOW_TO_LABEL.md").exists()
    meta = json.loads((made / "inventory.json").read_text(encoding="utf-8"))
    assert meta["glyphs"] == 6 and meta["clusters"] == 3
    assert meta["pages"] == [1]


def test_the_label_page_embeds_its_images_and_offers_every_group(made: Path) -> None:
    """Outcome: one row per group, images inline, so the page works opened from disk."""
    page = (made / "label.html").read_text(encoding="utf-8")

    assert page.count('<tr data-id="') == 3
    assert "data:image/png;base64," in page
    assert "http://" not in page and "https://" not in page, "nothing is loaded from elsewhere"


def test_the_label_pages_script_is_script_not_escaped_html(made: Path) -> None:
    """Outcome: no HTML entity inside `<script>`, and the alphabet arrives as a string literal.

    Entities are not decoded inside a script. The first version escaped the alphabet for HTML, its
    quote became `&quot;`, the script did not parse, and no button on the page did anything.
    """
    import re
    import shutil
    import subprocess

    page = (made / "label.html").read_text(encoding="utf-8")
    (script,) = re.findall(r"<script>(.*?)</script>", page, flags=re.DOTALL)

    assert not re.search(r"&(quot|#x27|#39|amp|lt|gt);", script)
    assert 'new Set([..."' in script
    node = shutil.which("node")
    if node is not None:
        checked = made / "label_check.js"
        checked.write_text(script, encoding="utf-8")
        assert (
            subprocess.run(
                [node, "--check", str(checked)], capture_output=True, check=False
            ).returncode
            == 0
        )


def test_reader_settings_have_no_default(tmp_path: Path) -> None:
    """Outcome: a settings file that leaves one out is refused, naming it."""
    partial = tmp_path / "demo.sh"
    partial.write_text("GV_READER_LINE_MINIMUM_PT=6\n", encoding="utf-8")

    with pytest.raises(InventoryError, match="GV_READER_GLYPH_MAXIMUM_PT"):
        read_reader_settings(partial)


# ---------------------------------------------------------------------------
# Suggestions
# ---------------------------------------------------------------------------

SETTINGS = ShapeSettings(size_px=24, bezier_steps=8, stroke_px=1, dilate_px=1)


def test_a_suggestion_needs_a_clear_margin_over_a_different_character() -> None:
    """Outcome: with no margin required a suggestion is made; with an impossible one, none is.

    A ring rather than a straight stroke: a perfectly straight line has no width, and no reference
    character with width is its size — which is the rule that stops every stroke on a drawing being
    suggested as a `1`.
    """
    from tests.extraction.test_glyph_shapes import _polygon

    shape = describe(_polygon(24), run_height=Decimal(10), settings=SETTINGS)
    references = reference_shapes(settings=SETTINGS)

    made = suggest(
        shape,
        references,
        maximum_distance=Decimal(100),
        minimum_margin=Decimal(0),
        maximum_size_ratio=Decimal(100),
    )
    refused = suggest(
        shape,
        references,
        maximum_distance=Decimal(100),
        minimum_margin=Decimal(1000),
        maximum_size_ratio=Decimal(100),
    )

    assert made is not None and made.label in ALPHABET
    assert refused is None


def test_a_dot_is_never_given_a_suggestion() -> None:
    from tests.extraction.test_glyph_shapes import DOT

    dot = describe(DOT, run_height=Decimal(10), settings=SETTINGS)

    assert (
        suggest(
            dot,
            reference_shapes(settings=SETTINGS),
            maximum_distance=Decimal(100),
            minimum_margin=Decimal(0),
            maximum_size_ratio=Decimal(100),
        )
        is None
    )


def test_the_built_in_references_are_hershey_only() -> None:
    """Outcome: nothing is suggested from a font the repository does not ship and may not license."""
    sources = {reference.source.split(":")[0] for reference in reference_shapes(settings=SETTINGS)}

    assert sources == {"hershey"}


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def test_build_writes_a_hashed_template_set_with_who_and_when(made: Path, tmp_path: Path) -> None:
    _save_labels(made, _all_labelled(made))

    assert _build(made, tmp_path / "templates") == 0

    (target,) = (tmp_path / "templates").iterdir()
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["labelled_by"] == "a reviewer"
    assert manifest["labelled_on"] == date(2026, 10, 1).isoformat()
    assert manifest["pages"] == [1]
    assert target.name == manifest["sha256"][:12]
    templates = np.load(target / "templates.npz")
    assert len(templates["labels"]) == 6


def test_changing_one_label_changes_the_hash(made: Path, tmp_path: Path) -> None:
    """**Acceptance criterion.** Outcome: two sets that differ by one label have different hashes."""
    labels = _all_labelled(made)
    _save_labels(made, labels)
    _build(made, tmp_path / "first")
    first = next((tmp_path / "first").iterdir()).name

    changed = dict(labels)
    cluster = next(iter(changed))
    changed[cluster] = "7" if changed[cluster] != "7" else "4"
    _save_labels(made, changed)
    _build(made, tmp_path / "second")
    second = next((tmp_path / "second").iterdir()).name

    assert first != second


def test_a_suggestion_nobody_confirmed_makes_no_template(made: Path, tmp_path: Path) -> None:
    """**The rule that keeps a person in charge.** Outcome: an empty label is no template, even with a
    suggestion beside it."""
    rows = _clusters(made)
    labels = {row["cluster_id"]: NOT_A_CHARACTER for row in rows[1:]}
    _save_labels(made, labels)

    assert _build(made, tmp_path / "templates") == 0

    (target,) = (tmp_path / "templates").iterdir()
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert rows[0]["cluster_id"] not in {entry["cluster"] for entry in manifest["labels"]}
    assert manifest["clusters_unlabelled"] == 1


def test_a_label_outside_the_alphabet_is_refused(
    made: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    labels = _all_labelled(made)
    labels[next(iter(labels))] = "seven"
    _save_labels(made, labels)

    assert _build(made, tmp_path / "templates") == 2
    assert "not in the alphabet" in capsys.readouterr().err


def test_a_mixed_group_is_refused_until_it_is_split(
    made: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    labels = _all_labelled(made)
    _save_labels(made, labels, mixed=frozenset({next(iter(labels))}))

    assert _build(made, tmp_path / "templates") == 2
    assert "marked mixed" in capsys.readouterr().err


def test_a_scored_crop_whose_shapes_are_unlabelled_is_refused(
    made: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """**Acceptance criterion.** Outcome: the build names the crop and the clusters it still needs."""
    glyphs = json.loads((made / "glyphs.json").read_text(encoding="utf-8"))
    left = min(glyph["box_px"][0] for glyph in glyphs) - 2
    top = min(glyph["box_px"][1] for glyph in glyphs) - 2
    right = max(glyph["box_px"][2] for glyph in glyphs) + 2
    bottom = max(glyph["box_px"][3] for glyph in glyphs) + 2
    key = tmp_path / "crops.csv"
    with key.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["crop_id", "page", "left_px", "top_px", "right_px", "bottom_px", "value"])
        writer.writerow(["k99", 1, left, top, right, bottom, '28"'])
    rows = _clusters(made)
    _save_labels(made, {row["cluster_id"]: NOT_A_CHARACTER for row in rows[1:]})

    assert _build(made, tmp_path / "templates", "--key", str(key)) == 2
    error = capsys.readouterr().err
    assert "k99" in error and rows[0]["cluster_id"] in error


def test_the_two_words_are_in_the_alphabet() -> None:
    assert {NOT_A_CHARACTER, SIDEWAYS} <= ALPHABET
    assert "seven" not in ALPHABET


def test_a_straight_stroke_is_not_suggested_as_a_character() -> None:
    """Outcome: no suggestion for a straight line, however loose the distance and margin.

    Shape alone cannot say whether a vertical stroke is a `1`, an inch tick or a piece of the
    drawing, and most of the client's strokes are the drawing. A person decides, with the stroke's
    context in front of them.
    """
    from tests.extraction.test_glyph_shapes import VERTICAL

    shape = describe(VERTICAL, run_height=Decimal(10), settings=SETTINGS)

    assert (
        suggest(
            shape,
            reference_shapes(settings=SETTINGS),
            maximum_distance=Decimal(100),
            minimum_margin=Decimal(0),
            maximum_size_ratio=Decimal(100),
        )
        is None
    )


def test_a_slanted_stroke_is_not_suggested_as_a_slash() -> None:
    """Outcome: a straight slanted stroke, which has width and height, still gets no suggestion.

    It is exactly a font's `/`, so the suggestion would be confident; on the client's drawing most
    such strokes are dimension-line ticks and hatching.
    """
    from extraction.annotations import PathSegment, SegmentKind, VectorPath

    slant = VectorPath(
        segments=(
            PathSegment(SegmentKind.MOVE, (Decimal(0), Decimal(0)), False),
            PathSegment(SegmentKind.LINE, (Decimal(4), Decimal(10)), False),
        ),
        stroked=True,
        filled=False,
    )
    shape = describe(slant, run_height=Decimal(10), settings=SETTINGS)

    assert (
        suggest(
            shape,
            reference_shapes(settings=SETTINGS),
            maximum_distance=Decimal(100),
            minimum_margin=Decimal(0),
            maximum_size_ratio=Decimal(100),
        )
        is None
    )


def test_characters_inside_an_answer_keys_crops_never_become_shapes_to_label(
    _inventory_once: Path, tmp_path: Path
) -> None:
    """**What keeps phase D honest.** Outcome: a crop over the first label leaves only the second.

    The scored readings must not be among the shapes a person labelled, or the scorecard measures
    the labeller's memory of the key rather than the reader.
    """
    glyphs = json.loads((_inventory_once / "glyphs.json").read_text(encoding="utf-8"))
    first_label = [glyph for glyph in glyphs if glyph["region"] == 0]
    key = tmp_path / "crops.csv"
    with key.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["crop_id", "page", "left_px", "top_px", "right_px", "bottom_px"])
        writer.writerow(
            [
                "k01",
                1,
                min(glyph["box_px"][0] for glyph in first_label),
                min(glyph["box_px"][1] for glyph in first_label),
                max(glyph["box_px"][2] for glyph in first_label),
                max(glyph["box_px"][3] for glyph in first_label),
            ]
        )
    out = tmp_path / "excluded"
    arguments = [
        "inventory",
        str(_inventory_once.parent / "shop.pdf"),
        "--pages",
        "1",
        "--out",
        str(out),
        "--reader-settings",
        str(_inventory_once.parent / "demo.sh"),
        "--exclude-crops",
        str(key),
        *SHAPE_ARGUMENTS,
    ]

    assert main(arguments) == 0

    meta = json.loads((out / "inventory.json").read_text(encoding="utf-8"))
    assert meta["glyphs"] == len(glyphs) - len(first_label)
    assert meta["skipped"]["inside an excluded answer-key crop"] == len(first_label)
    assert meta["excluded_crops"] == "crops.csv"
