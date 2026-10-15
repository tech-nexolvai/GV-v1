#!/usr/bin/env python3
"""Anant's Yes/No page for the keyed line answers, and his answers turned into confirmed keys (#1321).

**Why.** Almost every row answer in the keys was made by Claude (box numbers mapped by eye), and
the AI under test is Claude too, so a shared misreading would grade itself right. Anant decided
(2026-10-15, D6): he confirms each keyed line once, by clicking, on a local page; until then the
answers stay labelled Claude-made.

**page** writes one self-contained HTML file with one card per keyed line answer (one per question;
the same answer in several keys is one card, and two keys disagreeing is refused). Picture first,
as on Smriti's page (#1278):

1. **the stored row picture** the readers were shown (found by its SHA-256 under `--pictures` and
   checked), first as a close-up with the keyed line marked in yellow, then whole, with each
   numbered box clickable;
2. the page and set (`--set "AI Set 2=<database>"` reads page numbers from that database, read-only;
   or `--page-names` gives them), and which keys hold the answer;
3. one plain question, and **Yes / No / A different line**. After No: "No countertop on this page",
   "The right line has no box" or "Not sure". After "A different line": click its box.

A card whose picture cannot be found, or whose boxes are not proven, says so and has no buttons.
Answers are kept in the browser while working; **Copy answers** gives the `pariksha-row-answers/1`
text to save to a file for `apply`.

**apply** turns the answers into new key versions (a key is never edited in place): a Yes marks the
case `confirmed_by: <who>`; a different line or a No replaces the answer and marks who; Not sure
changes nothing. Every answer names the truth it was shown, and one the key no longer holds is
refused. With `--update-library` the list's entries are replaced by the new versions (the old list
is kept beside it).

    python scripts/pariksha_row_review_page.py page --library ~/gv-local/keys/library.json \\
        --row-boxes ~/gv-local/keys/<box map>.json --pictures ~/gv-local/<store> \\
        [--set "AI Set 1=<database>"]... [--out ~/gv-local/keys/review/rows-<date>.html]

    python scripts/pariksha_row_review_page.py apply --answers ~/gv-local/keys/review/<answers>.json \\
        --library ~/gv-local/keys/library.json --row-boxes ~/gv-local/keys/<box map>.json \\
        [--who anant] [--update-library]

The page, the answers and the keys hold client pictures and values: written under `~/gv-local/`,
once, never inside a git working tree. No model call; databases are only read.

Exit codes: 0 done, 2 refused.

Source: issue #1321 · Verification: `tests/scripts/test_pariksha_row_review_page.py`
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import io
import json
import re
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pariksha_scorecard import REFUSED, Refused, _database_url, _outside_git

from eval.pariksha.keys import (
    AnswerKey,
    KeyCase,
    KeyFormatError,
    QuestionIdentity,
    RowTruth,
    dump_key,
    load_key,
    read_key,
)
from eval.pariksha.row_geometry import (
    PictureBoxes,
    RowBoxes,
    RowBoxesError,
    on_picture,
    read_row_boxes,
)
from eval.smriti.library import (
    Library,
    LibraryEntry,
    LibraryError,
    describe,
    library_path,
    load_entry_key,
    read_library,
    register,
    write_library,
)

ANSWERS_FORMAT: Final = "pariksha-row-answers/1"
SAID: Final = ("yes", "other_line", "no_countertop", "not_among_boxes", "unsure")
_ANSWER_FIELDS: Final = frozenset(
    {"page_sha256", "question_id", "shown_truth", "picture_sha256", "said", "box", "note"}
)
_CLOSE_UP_WIDTH: Final = 1000


# --- the keyed answers -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Holder:
    key: AnswerKey
    path: Path
    entry: LibraryEntry | None


def _holders(args: argparse.Namespace) -> list[_Holder]:
    if args.library is not None and args.key:
        raise Refused("give --library or --key, not both")
    holders: list[_Holder] = []
    if args.key:
        for path in args.key:
            key = read_key(path.expanduser())
            if not isinstance(key, AnswerKey) or key.agent != "rekha":
                raise Refused(f"{path} is not a Rekha rows key")
            holders.append(_Holder(key, path.expanduser().resolve(), None))
        return holders
    library = read_library(library_path(args.library))
    for entry in library.entries:
        if entry.agent != "rekha":
            continue
        loaded = load_entry_key(entry)
        assert isinstance(loaded, AnswerKey)
        holders.append(_Holder(loaded, Path(entry.path), entry))
    if not holders:
        raise Refused("the library holds no Rekha rows key")
    return holders


@dataclass(frozen=True, slots=True)
class _Card:
    question: QuestionIdentity
    truth: RowTruth
    picture_sha256: str | None
    tolerance: Decimal | None
    holders: tuple[_Holder, ...]


def _cards(holders: Sequence[_Holder]) -> list[_Card]:
    found: dict[QuestionIdentity, _Card] = {}
    for holder in holders:
        for case in holder.key.cases:
            assert isinstance(case.truth, RowTruth)
            known = found.get(case.question)
            if known is None:
                found[case.question] = _Card(
                    case.question,
                    case.truth,
                    case.picture_sha256,
                    holder.key.row_tolerance_pt,
                    (holder,),
                )
            elif (known.truth, known.picture_sha256) != (case.truth, case.picture_sha256):
                raise Refused(
                    f"{case.question}: {known.holders[0].key.name} and {holder.key.name} give it "
                    f"different answers ({known.truth}, {case.truth}); settle that first"
                )
            else:
                found[case.question] = replace(known, holders=(*known.holders, holder))
    return sorted(found.values(), key=lambda c: (c.question.page_sha256, c.question.question_id))


# --- pictures --------------------------------------------------------------------------------


def _picture_files(roots: Sequence[Path]) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for root in roots:
        for path in Path(root).expanduser().rglob("*.png"):
            sha = path.stem.removeprefix("rows-")
            if len(sha) == 64:
                found.setdefault(sha, path)
    return found


def _read_picture(files: Mapping[str, Path], sha: str) -> bytes | None:
    path = files.get(sha)
    if path is None:
        return None
    data = path.read_bytes()
    return data if hashlib.sha256(data).hexdigest() == sha else None


@dataclass(frozen=True, slots=True)
class _Shown:
    picture: PictureBoxes | None
    png: bytes | None
    keyed_box: int | None
    problem: str


def _choose(card: _Card, row_boxes: RowBoxes, files: Mapping[str, Path]) -> _Shown:
    """The picture the card shows: the one a box-number answer belongs to, else the page's proven
    picture whose file is found, preferring one where the keyed line has a box."""
    if card.picture_sha256 is not None:
        options = [card.picture_sha256]
    else:
        options = sorted(p.picture_sha256 for p in row_boxes.for_page(card.question.page_sha256))
    best: _Shown | None = None
    for sha in options:
        png = _read_picture(files, sha)
        picture = row_boxes.get(sha, card.question.page_sha256)
        if png is None or picture is None:
            continue
        keyed: int | None = None
        if card.truth.at is not None and card.tolerance is not None:
            here = on_picture(card.truth.at, picture, card.tolerance)
            keyed = here.box if isinstance(here, RowTruth) else None
        elif card.truth.kind == "row":
            keyed = card.truth.box
        shown = _Shown(picture, png, keyed, "")
        if keyed is not None or card.truth.kind != "row":
            return shown
        best = best or shown
    if best is not None:
        return best
    return _Shown(
        None,
        None,
        None,
        "The stored picture for this page was not found (or its boxes are not proven), so it "
        "cannot be checked here.",
    )


def _png_size(png: bytes) -> tuple[int, int]:
    return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")


def _close_up(png: bytes, picture: PictureBoxes, box: int) -> bytes | None:
    """The keyed line's neighbourhood, enlarged, with the line marked in yellow."""
    if picture.px_per_pt is None:
        return None
    from PIL import Image, ImageDraw

    scale = float(picture.px_per_pt)
    line = picture.boxes[box - 1]
    x0, x1, y = float(line.x0) * scale, float(line.x1) * scale, float(line.y) * scale
    image = Image.open(io.BytesIO(png)).convert("RGBA")
    width = max(x1 - x0, 60.0)
    margin_x, margin_y = max(160.0, 0.4 * width), max(120.0, 0.35 * width)
    left, right = max(0, int(x0 - margin_x)), min(image.width, int(x1 + margin_x))
    top, bottom = max(0, int(y - margin_y)), min(image.height, int(y + margin_y))
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    # Outside the numbered box, so the box's own colour and the printed numbers stay visible.
    band = (x0 - 16, y - 20, x1 + 16, y + 20)
    draw.rectangle(band, fill=(255, 221, 0, 40))
    draw.rectangle(band, outline=(0, 0, 0, 255), width=5)
    draw.rectangle(
        (band[0] + 1, band[1] + 1, band[2] - 1, band[3] - 1), outline=(255, 200, 0, 255), width=3
    )
    crop = Image.alpha_composite(image, overlay).crop((left, top, right, bottom)).convert("RGB")
    factor = min(2.5, _CLOSE_UP_WIDTH / max(crop.width, 1))
    if factor > 1:
        crop = crop.resize(
            (int(crop.width * factor), int(crop.height * factor)), Image.Resampling.LANCZOS
        )
    out = io.BytesIO()
    crop.save(out, format="PNG")
    return out.getvalue()


# --- the page --------------------------------------------------------------------------------


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _data_uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode()


def _question(card: _Card) -> str:
    if card.truth.kind == "no_countertop":
        return "Is it right that this page has no countertop to check?"
    if card.truth.kind == "not_among_boxes":
        return (
            "Is it right that the countertop's line of numbers has no numbered box around it "
            "on this picture?"
        )
    return (
        "Is the line marked in yellow the countertop's line of numbers (the piece widths "
        "under the stone)?"
    )


def _boxes_html(shown: _Shown) -> str:
    """The whole picture, with each numbered box clickable (when the boxes can be placed)."""
    assert shown.png is not None and shown.picture is not None
    width, height = _png_size(shown.png)
    buttons = []
    if shown.picture.px_per_pt is not None and width and height:
        scale = float(shown.picture.px_per_pt)
        for number, line in enumerate(shown.picture.boxes, start=1):
            x0, x1 = float(line.x0) * scale - 6, float(line.x1) * scale + 6
            y0, y1 = float(line.y) * scale - 10, float(line.y) * scale + 10
            style = (
                f"left:{100 * x0 / width:.3f}%;top:{100 * y0 / height:.3f}%;"
                f"width:{100 * (x1 - x0) / width:.3f}%;height:{100 * (y1 - y0) / height:.3f}%"
            )
            keyed = " keyed" if number == shown.keyed_box else ""
            buttons.append(
                f'<button class="box{keyed}" data-box="{number}" style="{style}" '
                f'aria-pressed="false" title="box {number}" aria-label="box {number}">'
                f"<span>{number}</span></button>"
            )
    return (
        '<figure><div class="whole">'
        f'<img alt="the whole page as the readers saw it, with its numbered boxes" '
        f'src="{_data_uri(shown.png)}">{"".join(buttons)}</div>'
        "<figcaption>The whole page as the AI saw it. To name a different line, press "
        "<b>A different line</b>, then click its box.</figcaption></figure>"
    )


def _card_html(number: int, card: _Card, shown: _Shown, where: str) -> str:
    keys = ", ".join(sorted(holder.key.name for holder in card.holders))
    trust = sorted({holder.entry.label for holder in card.holders if holder.entry is not None}) or [
        "Claude-made, not yet confirmed by a person"
    ]
    confirmed = [
        case.confirmed_by
        for holder in card.holders
        for case in holder.key.cases
        if case.question == card.question and case.confirmed_by
    ]
    status = (
        f"Already confirmed by {', '.join(sorted(set(confirmed)))}"
        if confirmed
        else "Claude-made: not yet confirmed by a person"
    )
    head = (
        f'<div class="head"><span class="num">{number}</span>'
        f'<span class="where">{_e(where)}</span>'
        f'<span class="badge">{_e(status)}</span></div>'
    )
    details = (
        f"<details><summary>details</summary><code>question {_e(card.question)}; key answer "
        f"{_e(card.truth)}; keys: {_e(keys)}; trust: {_e('; '.join(trust))}"
        + ("" if shown.picture is None else f"; picture {_e(shown.picture.picture_sha256)}")
        + "</code></details>"
    )
    if shown.png is None or shown.picture is None:
        return (
            f'<section class="card" data-case="{number}">{head}'
            f'<p class="problem">{_e(shown.problem)}</p>{details}</section>'
        )
    figures = []
    if shown.keyed_box is not None and card.truth.kind == "row":
        close = _close_up(shown.png, shown.picture, shown.keyed_box)
        if close is not None:
            figures.append(
                f'<figure><img alt="close-up of the keyed line, marked in yellow" '
                f'src="{_data_uri(close)}"><figcaption>Close-up: the yellow band is the line '
                f"the key says is the countertop's (box {shown.keyed_box}).</figcaption></figure>"
            )
    elif card.truth.kind == "row":
        figures.append(
            '<p class="problem">The keyed line has no box on this picture; look at the whole '
            "page below.</p>"
        )
    figures.append(_boxes_html(shown))
    meta = {
        "page_sha256": card.question.page_sha256,
        "question_id": card.question.question_id,
        "shown_truth": str(card.truth),
        "picture_sha256": shown.picture.picture_sha256,
        "boxes": len(shown.picture.boxes),
    }
    no_countertop = (
        ""
        if card.truth.kind == "no_countertop"
        else '<button data-said="no_countertop" aria-pressed="false">'
        "No countertop on this page</button>"
    )
    no_box = (
        ""
        if card.truth.kind == "not_among_boxes"
        else '<button data-said="not_among_boxes" aria-pressed="false">'
        "The right line has no box</button>"
    )
    return (
        f'<section class="card" data-case="{number}" data-meta="{_e(json.dumps(meta))}">{head}'
        f'<div class="pics">{"".join(figures)}</div>'
        f'<p class="ask">{_e(_question(card))}</p>'
        '<div class="row verdict">'
        '<button class="yes" data-v="yes" aria-pressed="false">Yes</button>'
        '<button class="no" data-v="no" aria-pressed="false">No</button>'
        '<button class="other" data-v="other_line" aria-pressed="false">A different line</button>'
        "</div>"
        f'<div class="row follow" hidden><span class="label">What is right?</span>{no_countertop}'
        f'{no_box}<button data-said="unsure" aria-pressed="false">Not sure</button></div>'
        '<p class="pick" hidden>Now click the right line\'s box on the whole page above.</p>'
        '<p class="status" aria-live="polite"></p>'
        f"{details}</section>"
    )


_STYLE: Final = """
:root { --bg:#f6f6f4; --card:#fff; --ink:#1b1b1b; --muted:#5b5b5b; --line:#d9d9d4; --yes:#1f7a3a;
  --no:#b3261e; --other:#2a5bd7; --warnbg:#fde2e2; --warn:#9b1c1c; --accent:#6b3fd6; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg:#151515;
  --card:#1f1f1f; --ink:#ececec; --muted:#a8a8a8; --line:#3a3a3a; --yes:#4cc070; --no:#ff7b72;
  --other:#7aa2ff; --warnbg:#3d1717; --warn:#ff9b9b; --accent:#a98bff; } }
:root[data-theme="dark"] { --bg:#151515; --card:#1f1f1f; --ink:#ececec; --muted:#a8a8a8;
  --line:#3a3a3a; --yes:#4cc070; --no:#ff7b72; --other:#7aa2ff; --warnbg:#3d1717; --warn:#ff9b9b;
  --accent:#a98bff; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
  font:18px/1.5 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; }
main { max-width:1100px; margin:0 auto; padding:16px; }
h1 { font-size:26px; margin:8px 0; }
.intro, .muted, figcaption { color:var(--muted); }
.bar { position:sticky; top:0; z-index:5; background:var(--bg); border-bottom:1px solid var(--line);
  padding:10px 16px; display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px;
  margin:18px 0; }
.card.done-yes { border-left:8px solid var(--yes); } .card.done-no { border-left:8px solid var(--no); }
.card.done-other { border-left:8px solid var(--other); }
.head { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-bottom:8px; }
.num { font-weight:700; font-size:22px; color:var(--accent); }
.where { font-weight:700; }
.badge { font-size:14px; padding:2px 10px; border-radius:999px; background:var(--line); }
.pics { display:grid; gap:12px; } figure { margin:0; } figcaption { font-size:14px; }
img { width:100%; height:auto; border:1px solid var(--line); border-radius:6px; background:#fff;
  display:block; }
.whole { position:relative; }
.whole .box { position:absolute; padding:0; border:2px dashed transparent; background:transparent;
  border-radius:4px; cursor:default; }
.whole .box span { display:none; }
.card.picking .whole .box { border-color:var(--other); cursor:pointer; background:rgba(42,91,215,.08); }
.card.picking .whole .box span { display:inline; position:absolute; left:-1.6em; top:-0.2em;
  background:var(--other); color:#fff; font-size:13px; padding:0 5px; border-radius:6px; }
.whole .box.on { border:3px solid var(--other); background:rgba(42,91,215,.18); }
.whole .box.on span { display:inline; position:absolute; left:-1.6em; top:-0.2em;
  background:var(--other); color:#fff; font-size:13px; padding:0 5px; border-radius:6px; }
.problem { background:var(--warnbg); color:var(--warn); padding:8px 12px; border-radius:8px; }
.ask { font-size:20px; font-weight:600; margin:14px 0 6px; }
.row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin:8px 0; }
.label { color:var(--muted); }
button { font:inherit; font-weight:600; padding:8px 18px; border-radius:10px; cursor:pointer;
  border:2px solid var(--line); background:var(--card); color:var(--ink); }
.verdict button { font-size:22px; padding:10px 30px; }
button.on { background:var(--other); border-color:var(--other); color:#fff; }
.verdict button.yes.on { background:var(--yes); border-color:var(--yes); }
.verdict button.no.on { background:var(--no); border-color:var(--no); }
.pick { color:var(--other); font-weight:600; }
.status { color:var(--muted); font-size:15px; margin:4px 0; }
details { margin-top:10px; color:var(--muted); font-size:14px; }
code { font-size:13px; word-break:break-all; }
.copy { background:var(--accent); color:#fff; border-color:var(--accent); }
textarea#out { width:100%; min-height:160px; display:none; font:13px monospace; }
@media (max-width:600px) { main { padding:12px; } .verdict button { padding:10px 18px; } }
"""

_SCRIPT: Final = """
(function () {
  var META = JSON.parse(document.getElementById("meta").textContent);
  var STORE = "gv-row-answers-" + META.cards_sha256;
  var state = {};
  try { state = JSON.parse(localStorage.getItem(STORE) || "{}") || {}; } catch (e) { state = {}; }
  function save() { try { localStorage.setItem(STORE, JSON.stringify(state)); } catch (e) {} }
  var cards = Array.prototype.slice.call(document.querySelectorAll(".card[data-meta]"));
  function mark(b, on) { b.classList.toggle("on", on); b.setAttribute("aria-pressed", on ? "true" : "false"); }
  function said(a) {
    if (a.v === "yes") return "yes";
    if (a.v === "other_line") return a.box ? "other_line" : null;
    if (a.v === "no") return a.said || null;
    return null;
  }
  function paint(card) {
    var id = card.getAttribute("data-case"), a = state[id] || {};
    card.querySelectorAll("button[data-v]").forEach(function (b) { mark(b, a.v === b.getAttribute("data-v")); });
    card.querySelectorAll("button[data-said]").forEach(function (b) { mark(b, a.said === b.getAttribute("data-said")); });
    card.querySelectorAll("button[data-box]").forEach(function (b) {
      mark(b, a.v === "other_line" && String(a.box) === b.getAttribute("data-box")); });
    card.querySelector(".follow").hidden = a.v !== "no";
    card.querySelector(".pick").hidden = a.v !== "other_line" || !!a.box;
    card.classList.toggle("picking", a.v === "other_line");
    var s = said(a);
    card.classList.toggle("done-yes", s === "yes");
    card.classList.toggle("done-no", !!s && s !== "yes" && s !== "other_line");
    card.classList.toggle("done-other", s === "other_line");
    var status = card.querySelector(".status");
    status.textContent = s === null ? (a.v ? "Still needed: one more click." : "")
      : s === "other_line" ? "Answer: box " + a.box + " is the right line."
      : s === "yes" ? "Answer: yes." : s === "unsure" ? "Answer: not sure (nothing changes)."
      : s === "no_countertop" ? "Answer: no countertop on this page."
      : "Answer: the right line has no box.";
  }
  function count() {
    var done = cards.filter(function (c) { return said(state[c.getAttribute("data-case")] || {}) !== null; }).length;
    document.getElementById("done").textContent = done + " of " + cards.length + " answered";
  }
  cards.forEach(function (card) {
    var id = card.getAttribute("data-case");
    card.addEventListener("click", function (event) {
      var b = event.target.closest("button"); if (!b) return;
      var a = state[id] = state[id] || {};
      if (b.hasAttribute("data-v")) { a.v = b.getAttribute("data-v"); delete a.said; delete a.box; }
      else if (b.hasAttribute("data-said")) { a.said = b.getAttribute("data-said"); }
      else if (b.hasAttribute("data-box")) { if (a.v !== "other_line") return;
        a.box = parseInt(b.getAttribute("data-box"), 10); }
      save(); paint(card); count();
    });
    paint(card);
  });
  count();
  document.getElementById("copy").addEventListener("click", function () {
    var answers = [];
    cards.forEach(function (card) {
      var a = state[card.getAttribute("data-case")] || {}, s = said(a); if (s === null) return;
      var m = JSON.parse(card.getAttribute("data-meta"));
      var out = { page_sha256: m.page_sha256, question_id: m.question_id, shown_truth: m.shown_truth,
        picture_sha256: m.picture_sha256, said: s };
      if (s === "other_line") out.box = a.box;
      answers.push(out);
    });
    var text = JSON.stringify({ format: META.answers_format, answers: answers }, null, 1);
    var area = document.getElementById("out"); area.style.display = "block"; area.value = text;
    area.select();
    var told = document.getElementById("copied");
    function done(ok) { told.textContent = ok ? "Copied " + answers.length + " answers." :
      "Select the text below and copy it."; }
    if (navigator.clipboard) { navigator.clipboard.writeText(text).then(function () { done(true); },
      function () { done(false); }); } else { try { done(document.execCommand("copy")); } catch (e) { done(false); } }
  });
})();
"""


def build_page(cards: Sequence[tuple[_Card, _Shown, str]], *, day: str) -> str:
    sections = [
        _card_html(number, card, shown, where)
        for number, (card, shown, where) in enumerate(cards, start=1)
    ]
    cards_sha256 = hashlib.sha256("".join(sections).encode()).hexdigest()
    meta = {"answers_format": ANSWERS_FORMAT, "cards_sha256": cards_sha256}
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Line Check</title><style>{_STYLE}</style></head><body>"
        '<div class="bar"><b>Line check</b><span id="done"></span>'
        '<button class="copy" id="copy">Copy answers</button><span id="copied" aria-live="polite">'
        "</span></div><main>"
        "<h1>Which line is the countertop's?</h1>"
        '<p class="intro">Each card is one page the line finder was asked about. The answer on '
        "each card was made by Claude, and the AI being tested is also Claude, so please check "
        "each one yourself. Look at the picture, then click <b>Yes</b> if the yellow line is the "
        "countertop's line of numbers, <b>No</b> if it is not, or <b>A different line</b> and then "
        "click the right box. When you are done, press <b>Copy answers</b> (top) and paste the "
        f"text into a file for Claude. Made {_e(day)}; your clicks are kept in this browser until "
        "you copy them.</p>"
        f'{"".join(sections)}<textarea id="out" aria-label="answers to copy"></textarea></main>'
        f'<script type="application/json" id="meta">{json.dumps(meta)}</script>'
        f"<script>{_SCRIPT}</script></body></html>\n"
    )


def _page_names(args: argparse.Namespace) -> dict[str, str]:
    names: dict[str, str] = {}
    if args.page_names is not None:
        data = json.loads(args.page_names.expanduser().read_text())
        if not isinstance(data, Mapping):
            raise Refused("--page-names is a JSON object: page hash -> name")
        names.update({str(page): str(name) for page, name in data.items()})
    for value in args.set or ():
        label, _, database = value.partition("=")
        if not label.strip() or not database.strip():
            raise Refused(f"--set {value!r}: give 'name=database'")
        from sqlalchemy import create_engine, select, text
        from sqlalchemy.orm import Session

        from app.models.document import Page

        engine = create_engine(_database_url(database.strip()))
        try:
            with Session(engine) as session:
                session.execute(text("SET TRANSACTION READ ONLY"))
                for index, content_hash in session.execute(select(Page.index, Page.content_hash)):
                    names.setdefault(content_hash, f"{label.strip()}, page {index + 1}")
                session.rollback()
        finally:
            engine.dispose()
    return names


def _natural(name: str) -> tuple[tuple[int, str], ...]:
    """Natural order: "AI Set 2, page 9" before "AI Set 2, page 12"."""
    return tuple(
        (int(part), "") if part.isdigit() else (0, part) for part in re.split(r"(\d+)", name)
    )


def _page(args: argparse.Namespace) -> int:
    day = args.day or datetime.now(UTC).date().isoformat()
    out = args.out or Path(f"~/gv-local/keys/review/rows-{day}.html")
    out = _outside_git(out)
    if out.exists():
        raise Refused(f"{out} already exists: the page is written once")
    holders = _holders(args)
    row_boxes = read_row_boxes(args.row_boxes)
    files = _picture_files(args.pictures)
    names = _page_names(args)
    cards = []
    for card in _cards(holders):
        page = card.question.page_sha256
        where = names.get(page, f"page {page[:12]}")
        cards.append((card, _choose(card, row_boxes, files), where))
    cards.sort(key=lambda item: (_natural(item[2]), item[0].question.question_id))
    text = build_page(cards, day=day)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x") as handle:
        handle.write(text)
    missing = sum(1 for _card, shown, _where in cards if shown.png is None)
    print(f"line check page: {len(cards)} card(s), {missing} without a usable picture")
    print(f"  written: {out}")
    return 0


# --- applying the answers --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Answer:
    question: QuestionIdentity
    shown_truth: str
    picture_sha256: str
    said: str
    box: int | None


def _read_answers(path: Path) -> list[_Answer]:
    try:
        data = json.loads(path.expanduser().read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise Refused(f"{path}: {error}") from error
    if not isinstance(data, Mapping) or data.get("format") != ANSWERS_FORMAT:
        raise Refused(f"{path} is not a {ANSWERS_FORMAT} file")
    raw = data.get("answers")
    if not isinstance(raw, list) or not raw:
        raise Refused("answers must be a non-empty list")
    answers: list[_Answer] = []
    seen: set[QuestionIdentity] = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, Mapping) or set(item) - _ANSWER_FIELDS:
            raise Refused(f"answer {index} is not an answer object")
        said = item.get("said")
        if said not in SAID:
            raise Refused(f"answer {index}: said must be one of {SAID}, not {said!r}")
        box = item.get("box")
        if said == "other_line" and (isinstance(box, bool) or not isinstance(box, int) or box < 1):
            raise Refused(f"answer {index}: a different line needs the box clicked")
        question = QuestionIdentity(
            family="slot-row-choice",
            page_sha256=str(item.get("page_sha256")),
            question_id=str(item.get("question_id")),
        )
        if question in seen:
            raise Refused(f"answer {index}: {question} is answered twice")
        seen.add(question)
        answers.append(
            _Answer(
                question=question,
                shown_truth=str(item.get("shown_truth")),
                picture_sha256=str(item.get("picture_sha256")),
                said=str(said),
                box=box if said == "other_line" else None,
            )
        )
    return answers


def _new_case(
    case: KeyCase, key: AnswerKey, answer: _Answer, row_boxes: RowBoxes, who: str, day: str
) -> KeyCase:
    note = f"{who} on the line check page ({day})"
    if answer.said == "yes":
        return replace(case, confirmed_by=who)
    if answer.said == "no_countertop":
        truth = RowTruth(box=None, kind="no_countertop")
        changed = replace(case, truth=truth, picture_sha256=None)
    elif answer.said == "not_among_boxes":
        changed = replace(
            case,
            truth=RowTruth(box=None, kind="not_among_boxes"),
            picture_sha256=answer.picture_sha256,
        )
    else:
        assert answer.box is not None
        picture = row_boxes.get(answer.picture_sha256, answer.question.page_sha256)
        if picture is None:
            raise Refused(f"{answer.question}: the picture answered on has no proven boxes")
        if answer.box > len(picture.boxes):
            raise Refused(
                f"{answer.question}: box {answer.box} is beyond the picture's {len(picture.boxes)}"
            )
        if key.row_tolerance_pt is None:
            raise Refused(
                f"{key.name} names no row by geometry; convert it first "
                "(scripts/pariksha_row_boxes.py convert)"
            )
        changed = replace(
            case,
            truth=RowTruth(box=None, kind="row", at=picture.boxes[answer.box - 1]),
            picture_sha256=None,
        )
    said = (
        f"box {answer.box} of picture {answer.picture_sha256[:12]}"
        if answer.said == "other_line"
        else answer.said
    )
    source = f"{case.source} | changed by {note}: {said} (was {case.truth})".lstrip(" |")
    return replace(changed, confirmed_by=who, source=source)


def _apply(args: argparse.Namespace) -> int:
    who = args.who.strip()
    if not who:
        raise Refused("--who must name the person")
    day = args.day or datetime.now(UTC).date().isoformat()
    answers = _read_answers(args.answers)
    holders = _holders(args)
    row_boxes = read_row_boxes(args.row_boxes) if args.row_boxes else RowBoxes(pictures={})
    by_question = {answer.question: answer for answer in answers}
    held = {case.question for holder in holders for case in holder.key.cases}
    for answer in answers:
        if answer.question not in held:
            raise Refused(f"{answer.question} is in no key given")
    written: list[tuple[_Holder, AnswerKey, Path, int]] = []
    for holder in holders:
        cases: list[KeyCase] = []
        confirmed = 0
        for case in holder.key.cases:
            given = by_question.get(case.question)
            if given is None:
                cases.append(case)
                continue
            answer = given
            if str(case.truth) != answer.shown_truth:
                raise Refused(
                    f"{case.question}: the answer was given to {answer.shown_truth!r}, but "
                    f"{holder.key.name} now says {case.truth}; make the page again"
                )
            if answer.said == "unsure":
                cases.append(case)
                continue
            cases.append(_new_case(case, holder.key, answer, row_boxes, who, day))
            confirmed += 1
        if not confirmed:
            continue
        geometry = any(isinstance(c.truth, RowTruth) and c.truth.at is not None for c in cases)
        new = load_key(
            dump_key(
                replace(
                    holder.key,
                    version=f"{holder.key.version}+confirmed-{day}",
                    cases=tuple(cases),
                    row_tolerance_pt=holder.key.row_tolerance_pt if geometry else None,
                )
            )
        )
        assert isinstance(new, AnswerKey)
        path = _outside_git(holder.path.with_name(f"{holder.path.stem}.confirmed-{day}.json"))
        if path.exists():
            raise Refused(f"{path} already exists: a key is never overwritten")
        written.append((holder, new, path, confirmed))
    if not written:
        print("nothing to change: every answer was 'not sure'")
        return 0
    for _holder, new, path, _count in written:
        with path.open("x") as handle:
            handle.write(json.dumps(dump_key(new), indent=1, ensure_ascii=False) + "\n")
    for holder, new, path, count in written:
        print(
            f"{new.name} {new.version}: {count} of {len(new.cases)} case(s) answered by {who}; "
            f"written: {path}"
        )
    if args.update_library:
        _update_library(args, written, who, day)
    return 0


def _update_library(
    args: argparse.Namespace,
    written: Sequence[tuple[_Holder, AnswerKey, Path, int]],
    who: str,
    day: str,
) -> None:
    if args.library is None and args.key:
        raise Refused("--update-library needs --library")
    path = library_path(args.library)
    library = read_library(path)
    replaced = {holder.entry for holder, *_rest in written if holder.entry is not None}
    updated = Library(
        path=library.path, entries=tuple(e for e in library.entries if e not in replaced)
    )
    for holder, new, new_path, count in written:
        old = holder.entry
        assert old is not None
        total = len(new.cases)
        every = sum(1 for case in new.cases if case.confirmed_by == who)
        entry, key = describe(
            new_path,
            confirmed_by=(
                f"{who} on the line check page ({day}): {every} of {total} case(s); the rest: "
                f"{old.confirmed_by}"
            ),
            label=(
                f"confirmed by {who} ({day})"
                if every == total
                else f"{every} of {total} confirmed by {who} ({day}); the rest: {old.label}"
            ),
            source=old.source,
        )
        updated = register(updated, entry, key)
    backup = path.with_name(f"{path.stem}.before-line-check-{day}{path.suffix}")
    if not backup.exists():
        shutil.copyfile(path, backup)
    write_library(updated)
    print(f"library updated: {len(written)} entr(ies) replaced; the old list kept as {backup}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name, handler, help_text in (
        ("page", _page, "write the Yes/No page"),
        ("apply", _apply, "turn the copied answers into confirmed key versions"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--library", type=Path, help="the key list (or set GV_KEY_LIBRARY)")
        command.add_argument("--key", type=Path, action="append", help="a rows key (repeatable)")
        command.add_argument("--row-boxes", type=Path, action="append", help="a box map")
        command.add_argument("--day", help="the date written on the page or the new versions")
        command.set_defaults(handler=handler)
        if name == "page":
            command.add_argument("--pictures", type=Path, action="append", required=True)
            command.add_argument("--page-names", type=Path, help="JSON: page hash -> name")
            command.add_argument("--set", action="append", help="'AI Set 1=<database>'")
            command.add_argument("--out", type=Path, help="the page (default under ~/gv-local)")
        else:
            command.add_argument("--answers", type=Path, required=True)
            command.add_argument("--who", default="anant", help="who answered (default anant)")
            command.add_argument("--update-library", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "page" and not args.row_boxes:
        print("pariksha line check: --row-boxes is needed to show the boxes", file=sys.stderr)
        return REFUSED
    try:
        return int(args.handler(args))
    except (
        Refused,
        KeyFormatError,
        LibraryError,
        RowBoxesError,
        FileExistsError,
    ) as error:
        print(f"pariksha line check: {error}", file=sys.stderr)
        return REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
