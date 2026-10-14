#!/usr/bin/env python3
"""Smriti's review page: a person confirms each candidate case by clicking, on a local page (#1271).

**Why.** A Smriti case becomes part of the ruler that accepts or rejects an AI change, so a person
looks at every one before it joins a key. Anant decided (2026-10-15): he confirms each case himself,
by clicking Yes / No / Unsure, never by typing blind, and never Claude (the AI under test is Claude,
so a shared misread would grade itself right).

**What it shows.** One card per *ready* candidate (`eval.smriti.confirm.ready_candidates`: a current,
convertible decision by a name on the local real-reviewer list, whose question a key takes), in this
order:

1. **the exact picture the AI saw**: the stored question packet's `images.*`, read from the artifact
   store by its `storage_key` and checked against the packet's `sha256` ($0: nothing is rendered or
   asked again). A piece kind shows the pictures its piece's label question showed. A card whose
   picture cannot be found or does not match says why, and has no buttons;
2. **the proposed answer and where it came from** (`answer_source`: `pdf-text`, the file's own text;
   `ai-reading`, the AI's reading; `decision`, the reviewer's own decision), or that the person
   states it from the picture;
3. **the reviewer's decision**, in one plain sentence;
4. **Yes / No / Unsure**, plus what that question needs: the box on the numbered picture, what the
   vendor drew at each wall end, a batch page's own answer, and for a correction "AI misread / vendor
   drew it wrong" with the printed text (pre-filled from the file's own text under the label where
   there is exactly one run).

**"Copy answers"** gives the `smriti-confirmations/2` JSON that `scripts/smriti_candidates.py confirm`
takes, bound to the candidates file by its hash. Answers are kept in the browser (local storage) so
a half-done page survives a reload. The page is one self-contained HTML file: no external scripts,
styles or fonts.

    python scripts/smriti_review_page.py ~/gv-local/smriti/<date>/<candidates>.json \\
        --db <database name or URL> --store <artifact store root> \\
        [--out ~/gv-local/smriti/<date>/review-<database>.html]

The page holds client pictures and values: it is written under `~/gv-local/smriti/<date>/` by
default, and a path inside a git working tree is refused. It is written once, never overwritten.
The database is only read (read-only transaction). No model call.

Exit codes: 0 done, 2 refused.

Source: issue #1271 · Verification: `tests/scripts/test_smriti_review_page.py`
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final
from uuid import UUID

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from pariksha_scorecard import REFUSED, Refused, _database_url, _outside_git
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from eval.pariksha.keys import FAMILIES
from eval.pariksha.kinds import KINDS_FAMILY
from eval.pariksha.stored import prompt_family
from eval.pariksha.walls import DRAWN, WALLS_FAMILY, _polygon_box
from eval.smriti.candidates import (
    MISTAKE_UNANSWERED,
    Candidate,
    CandidateExport,
    CandidatesFileError,
    read_candidates,
)
from eval.smriti.confirm import (
    CONFIRMATIONS_FORMAT,
    QuestionType,
    question_type,
    ready_candidates,
    site_ends,
)
from vocabulary.wall_layouts import WALL_LAYOUT_WORDS

__all__ = ["REFUSED", "Picture", "build_page", "default_out", "find_pictures", "main"]

LABEL_FAMILY: Final = FAMILIES["akshara"]
ROW_FAMILY: Final = FAMILIES["rekha"]
#: Box buttons offered when the stored question does not say how many rows it numbered.
UNKNOWN_BOXES: Final = 12
SOURCES: Final[Mapping[str, str]] = {
    "pdf-text": "the drawing file's own text under this label (read by code, not by an AI)",
    "ai-reading": (
        "the AI's own reading (the file has no text here), so check it letter by letter"
    ),
    "decision": "the reviewer's own decision",
}
DRAWN_WORDS: Final[Mapping[str, str]] = {
    "wall": "Wall drawn",
    "open": "Open, no wall",
    "not_shown": "Not shown",
    "key_unsure": "Can't tell",
}
TYPE_WORDS: Final[Mapping[str, str]] = {
    QuestionType.LABEL_PROPOSED: "printed number",
    QuestionType.LABEL_STATED: "corrected number",
    QuestionType.ROW_STATED: "no countertop",
    QuestionType.ROW_BOX: "there is a countertop",
    QuestionType.ROW_BATCH: "page from a batch click",
    QuestionType.WALLS: "walls",
    QuestionType.PIECE_KIND: "piece kind",
}


@dataclass(frozen=True, slots=True)
class Picture:
    """One stored picture a question showed the AI, or why it cannot be shown."""

    name: str
    sha256: str
    png: bytes | None
    problem: str | None


# --- the pictures, from the database and the store ($0) ---------------------------------------


@dataclass(frozen=True, slots=True)
class _Packet:
    family: str
    run_id: UUID | None
    page_sha256: str | None
    question_id: str
    candidate_ids: tuple[str, ...]
    count: int | None
    images: tuple[tuple[str, str, str | None], ...]
    """`(name, sha256, storage_key)` of each picture."""


def _packets(session: Session) -> list[_Packet]:
    from app.models.runs import ModelInvocation

    found: list[_Packet] = []
    for prompt_id, run_id, packet in session.execute(
        select(
            ModelInvocation.prompt_id,
            ModelInvocation.extraction_run_id,
            ModelInvocation.reader_question_packet,
        ).where(
            ModelInvocation.reader_question_packet.is_not(None),
            ModelInvocation.prompt_id.startswith(f"{LABEL_FAMILY}-v")
            | ModelInvocation.prompt_id.startswith(f"{ROW_FAMILY}-v")
            | ModelInvocation.prompt_id.startswith(f"{WALLS_FAMILY}-v"),
        )
    ):
        if not isinstance(packet, Mapping) or not isinstance(packet.get("question_id"), str):
            continue
        images = packet.get("images")
        pictures = tuple(
            sorted(
                (
                    str(name),
                    str(image["sha256"]),
                    image.get("storage_key") if isinstance(image.get("storage_key"), str) else None,
                )
                for name, image in (images.items() if isinstance(images, Mapping) else ())
                if isinstance(image, Mapping) and isinstance(image.get("sha256"), str)
            )
        )
        candidates = packet.get("candidate_ids")
        count = packet.get("candidate_count")
        found.append(
            _Packet(
                family=prompt_family(prompt_id),
                run_id=run_id,
                page_sha256=(
                    str(packet["source_page_sha256"])
                    if isinstance(packet.get("source_page_sha256"), str)
                    else None
                ),
                question_id=str(packet["question_id"]),
                candidate_ids=tuple(map(str, candidates)) if isinstance(candidates, list) else (),
                count=count if isinstance(count, int) else None,
                images=pictures,
            )
        )
    return found


def _flags(session: Session, candidate_id: str) -> tuple[list[str], object]:
    from app.models.evidence import ObservationCandidate

    try:
        found = session.get(ObservationCandidate, UUID(candidate_id))
    except ValueError:
        return [], None
    if found is None:
        return [], None
    return [flag for flag in found.ambiguity_flags or () if isinstance(flag, str)], found.polygon


def _asked(session: Session, packets: Sequence[_Packet], candidate: Candidate) -> list[_Packet]:
    """The stored questions that asked this candidate's question, by its own guard."""
    question = candidate.question or {}
    family = question.get("family")
    page = question.get("page_sha256")
    if family == KINDS_FAMILY:
        return _piece_packets(session, packets, candidate)
    same = [
        packet
        for packet in packets
        if packet.family == family
        and packet.page_sha256 == page
        and packet.question_id == question.get("question_id")
    ]
    if family == LABEL_FAMILY:
        box = f"slot-box:{question.get('slot_box')}"
        return [
            packet
            for packet in same
            if len(packet.candidate_ids) == 1 and box in _flags(session, packet.candidate_ids[0])[0]
        ]
    if family == ROW_FAMILY:
        picture = question.get("picture_sha256")
        return [packet for packet in same if any(sha == picture for _n, sha, _k in packet.images)]
    if family == WALLS_FAMILY:
        row_box = question.get("row_box")
        kept = []
        for packet in same:
            if len(packet.candidate_ids) != 1:
                continue
            found = _polygon_box(_flags(session, packet.candidate_ids[0])[1])
            if found is not None and ",".join(str(edge) for edge in found) == row_box:
                kept.append(packet)
        return kept
    return []


def _piece_packets(
    session: Session, packets: Sequence[_Packet], candidate: Candidate
) -> list[_Packet]:
    """A piece's label question in the run Bala's link names: the pictures the readers saw."""
    from app.models.drawing import PartConfirmation, PartProposalRow

    question = candidate.question or {}
    found = re.match(r"^p(\d+):piece(\d+)$", str(question.get("question_id")))
    if found is None or candidate.table != "part_confirmations":
        return []
    try:
        decision = UUID(candidate.decision_id)
    except ValueError:
        return []
    proposal = session.scalar(
        select(PartConfirmation.part_proposal_id).where(PartConfirmation.id == decision)
    )
    link = (
        None
        if proposal is None
        else session.scalar(
            select(PartProposalRow).where(PartProposalRow.part_proposal_id == proposal)
        )
    )
    if link is None:
        return []
    prefix = f"p{found.group(1)}:slot{found.group(2)}:"
    return [
        packet
        for packet in packets
        if packet.family == LABEL_FAMILY
        and packet.run_id == link.extraction_run_id
        and packet.page_sha256 == question.get("page_sha256")
        and packet.question_id.startswith(prefix)
    ]


def _read(root: Path, name: str, sha: str, key: str | None) -> Picture:
    from storage.hashing import ArtifactCorrupt, IntegrityRecordMissing
    from storage.local import LocalStore

    if key is None:
        return Picture(name, sha, None, "the question's picture was not stored (no storage key)")
    try:
        with LocalStore(root=root).get(key) as stored:
            data = stored.read()
    except (OSError, ValueError, ArtifactCorrupt, IntegrityRecordMissing) as error:
        # Shown on the card, never hidden: a case without its picture cannot be answered.
        return Picture(name, sha, None, f"the picture is not in the store ({type(error).__name__})")
    if hashlib.sha256(data).hexdigest() != sha:
        return Picture(name, sha, None, "the stored picture is not the one the question recorded")
    return Picture(name, sha, data, None)


def _pdf_text(session: Session, page_sha256: str, slot_box: str) -> list[str]:
    """The file's own text runs wholly inside a label's box on this page, distinct, sorted."""
    from app.models.document import Page
    from app.models.evidence import ObservationCandidate
    from app.models.runs import ExtractionRun
    from eval.smriti.export import PDF_TEXT_EXTRACTORS

    parts = slot_box.split(",")
    if len(parts) != 4 or not all(part.lstrip("-").isdigit() for part in parts):
        return []
    x0, y0, x1, y1 = (int(part) for part in parts)
    found: set[str] = set()
    for raw_text, polygon in session.execute(
        select(ObservationCandidate.raw_text, ObservationCandidate.polygon)
        .join(ExtractionRun, ExtractionRun.id == ObservationCandidate.extraction_run_id)
        .join(Page, Page.id == ObservationCandidate.page_id)
        .where(Page.content_hash == page_sha256, ExtractionRun.extractor.in_(PDF_TEXT_EXTRACTORS))
    ):
        box = _polygon_box(polygon)
        if box is None or not raw_text or not raw_text.strip():
            continue
        if x0 <= box[0] and y0 <= box[1] and box[2] <= x1 and box[3] <= y1:
            found.add(raw_text)
    return sorted(found)


def find_pictures(
    session: Session, store_root: Path, candidates: Sequence[Candidate]
) -> tuple[dict[str, list[Picture]], dict[str, int], dict[str, str]]:
    """For each candidate: the pictures its question showed the AI, how many rows the numbered
    picture holds (row questions), and the text to pre-fill (a label the person states)."""
    packets = _packets(session)
    pictures: dict[str, list[Picture]] = {}
    boxes: dict[str, int] = {}
    prefill: dict[str, str] = {}
    for candidate in candidates:
        asked = _asked(session, packets, candidate)
        if not asked:
            pictures[candidate.candidate_id] = [
                Picture(
                    "-",
                    "",
                    None,
                    "no stored question names this case, so the picture the AI saw is not known",
                )
            ]
        else:
            seen: dict[str, Picture] = {}
            for packet in asked:
                for name, sha, key in packet.images:
                    if sha not in seen or seen[sha].png is None:
                        seen[sha] = _read(store_root, name, sha, key)
            pictures[candidate.candidate_id] = sorted(seen.values(), key=lambda p: p.name)
        counts = {packet.count or len(packet.candidate_ids) for packet in asked}
        if (
            candidate.question
            and candidate.question.get("family") == ROW_FAMILY
            and len(counts) == 1
        ):
            boxes[candidate.candidate_id] = counts.pop()
        if question_type(candidate) == QuestionType.LABEL_STATED and candidate.question:
            texts = _pdf_text(
                session,
                str(candidate.question.get("page_sha256")),
                str(candidate.question.get("slot_box")),
            )
            typed = candidate.recorded.get("typed")
            if len(texts) == 1:
                prefill[candidate.candidate_id] = texts[0]
            elif isinstance(typed, str):
                prefill[candidate.candidate_id] = typed
    return pictures, boxes, prefill


# --- the page ------------------------------------------------------------------------------------

_STYLE: Final = """
:root { --bg:#f6f6f4; --card:#fff; --ink:#1b1b1b; --muted:#5b5b5b; --line:#d9d9d4; --yes:#1f7a3a;
  --no:#b3261e; --unsure:#8a5a00; --accent:#6b3fd6; --warnbg:#fde2e2; --warn:#9b1c1c; --pick:#2a5bd7; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg:#151515; --card:#1f1f1f;
  --ink:#ececec; --muted:#a8a8a8; --line:#3a3a3a; --yes:#4cc070; --no:#ff7b72; --unsure:#ffcf70;
  --accent:#a98bff; --warnbg:#3d1717; --warn:#ff9b9b; --pick:#7aa2ff; } }
:root[data-theme="dark"] { --bg:#151515; --card:#1f1f1f; --ink:#ececec; --muted:#a8a8a8; --line:#3a3a3a;
  --yes:#4cc070; --no:#ff7b72; --unsure:#ffcf70; --accent:#a98bff; --warnbg:#3d1717; --warn:#ff9b9b;
  --pick:#7aa2ff; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
  font:18px/1.5 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; }
main { max-width:1100px; margin:0 auto; padding:16px; }
h1 { font-size:26px; margin:8px 0; }
.intro, .muted { color:var(--muted); }
.bar { position:sticky; top:0; z-index:5; background:var(--bg); border-bottom:1px solid var(--line);
  padding:10px 16px; display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
.bar input { font:inherit; padding:6px 10px; border-radius:8px; border:1px solid var(--line);
  background:var(--card); color:var(--ink); }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px;
  margin:18px 0; }
.card.done-yes { border-left:8px solid var(--yes); } .card.done-no { border-left:8px solid var(--no); }
.card.done-unsure { border-left:8px solid var(--unsure); }
.head { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-bottom:8px; }
.num { font-weight:700; font-size:22px; color:var(--accent); }
.badge { font-size:14px; padding:2px 10px; border-radius:999px; background:var(--line); }
.pics { display:grid; gap:10px; }
.pics figure { margin:0; } .pics figcaption { font-size:14px; color:var(--muted); }
img { width:100%; height:auto; border:1px solid var(--line); border-radius:6px; background:#fff; }
.problem { background:var(--warnbg); color:var(--warn); padding:8px 12px; border-radius:8px; }
.proposal { font-size:20px; margin:12px 0 4px; }
.proposal b { font-size:22px; }
.source { color:var(--muted); font-size:15px; }
.decision { margin:10px 0; }
.ask { font-weight:600; }
.row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin:8px 0; }
.row .label { min-width:9ch; color:var(--muted); }
button { font:inherit; font-weight:600; padding:8px 18px; border-radius:10px; cursor:pointer;
  border:2px solid var(--line); background:var(--card); color:var(--ink); }
button.on { background:var(--pick); border-color:var(--pick); color:#fff; }
.verdict button { font-size:22px; padding:10px 30px; }
.verdict button.yes.on { background:var(--yes); border-color:var(--yes); }
.verdict button.no.on { background:var(--no); border-color:var(--no); }
.verdict button.unsure.on { background:var(--unsure); border-color:var(--unsure); color:#000; }
input.texts, input.note { font:inherit; padding:8px 10px; border-radius:8px; border:1px solid var(--line);
  background:var(--bg); color:var(--ink); min-width:220px; flex:1; }
.missing { color:var(--warn); font-weight:600; }
details { margin-top:10px; color:var(--muted); font-size:14px; }
code { font-size:13px; word-break:break-all; }
.copy { background:var(--accent); color:#fff; border-color:var(--accent); }
textarea#out { width:100%; min-height:160px; display:none; font:13px monospace; }
@media (max-width:600px) { main { padding:12px; } .verdict button { padding:10px 18px; } }
"""

_SCRIPT: Final = """
(function () {
  var META = JSON.parse(document.getElementById("meta").textContent);
  var KEY = "gv-smriti-answers-" + META.candidates_sha256;
  var state = {};
  try { state = JSON.parse(localStorage.getItem(KEY) || "{}") || {}; } catch (e) { state = {}; }
  function save() { try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {} }
  var cards = Array.prototype.slice.call(document.querySelectorAll(".card[data-id]"));
  function get(id) { state.answers = state.answers || {}; state.answers[id] = state.answers[id] || {};
    return state.answers[id]; }
  function needs(card, a) {
    var type = card.getAttribute("data-type"), out = [];
    if (a.v !== "yes") return out;
    if (type === "label-stated") {
      if (card.getAttribute("data-mistake-needed") === "1" && !a.mistake)
        out.push("AI misread or vendor drew it wrong");
      if ((a.mistake === "ai_misread" || card.getAttribute("data-mistake-needed") !== "1")
          && !a.no_label && !(a.texts && a.texts.trim())) out.push("the printed text, or No label");
    }
    if (type === "row-box" && a.box == null) out.push("the box");
    if (type === "row-batch" && a.box == null && !a.own) out.push("this page's own answer");
    if (type === "walls" && (!a.left || !a.right)) out.push("what is drawn at each end");
    return out;
  }
  function paint(card) {
    var id = card.getAttribute("data-id"), a = get(id);
    card.querySelectorAll("button[data-v]").forEach(function (b) {
      b.classList.toggle("on", a.v === b.getAttribute("data-v")); });
    card.querySelectorAll("button[data-mistake]").forEach(function (b) {
      b.classList.toggle("on", a.mistake === b.getAttribute("data-mistake")); });
    card.querySelectorAll("button[data-box]").forEach(function (b) {
      b.classList.toggle("on", String(a.box) === b.getAttribute("data-box")); });
    card.querySelectorAll("button[data-own]").forEach(function (b) {
      b.classList.toggle("on", a.own === b.getAttribute("data-own")); });
    card.querySelectorAll("button[data-drawn]").forEach(function (b) {
      b.classList.toggle("on", a[b.getAttribute("data-end")] === b.getAttribute("data-drawn")); });
    card.querySelectorAll("button[data-nolabel]").forEach(function (b) {
      b.classList.toggle("on", !!a.no_label); });
    ["yes", "no", "unsure"].forEach(function (v) { card.classList.toggle("done-" + v, a.v === v); });
    var t = card.querySelector("input.texts");
    if (t) { if (a.texts == null) a.texts = t.getAttribute("data-prefill") || "";
      if (document.activeElement !== t) t.value = a.texts; t.disabled = !!a.no_label; }
    var n = card.querySelector("input.note"); if (n && document.activeElement !== n) n.value = a.note || "";
    var m = card.querySelector(".missing"); var miss = needs(card, a);
    if (m) m.textContent = miss.length ? "Still needed for Yes: " + miss.join(", ") : "";
  }
  function count() {
    var done = cards.filter(function (c) { var a = get(c.getAttribute("data-id"));
      return a.v && !needs(c, a).length; }).length;
    var el = document.getElementById("count");
    if (el) el.textContent = done + " of " + cards.length + " answered";
  }
  function toggle(a, field, value) { a[field] = a[field] === value ? null : value; }
  cards.forEach(function (card) {
    var id = card.getAttribute("data-id");
    card.addEventListener("click", function (e) {
      var b = e.target.closest("button"); if (!b) return; var a = get(id);
      if (b.hasAttribute("data-v")) toggle(a, "v", b.getAttribute("data-v"));
      else if (b.hasAttribute("data-mistake")) toggle(a, "mistake", b.getAttribute("data-mistake"));
      else if (b.hasAttribute("data-box")) { var x = b.getAttribute("data-box");
        a.box = String(a.box) === x ? null : (x === "not_among_boxes" ? x : parseInt(x, 10)); a.own = null; }
      else if (b.hasAttribute("data-own")) { toggle(a, "own", b.getAttribute("data-own")); a.box = null; }
      else if (b.hasAttribute("data-drawn")) toggle(a, b.getAttribute("data-end"), b.getAttribute("data-drawn"));
      else if (b.hasAttribute("data-nolabel")) a.no_label = !a.no_label;
      else return;
      save(); paint(card); count();
    });
    card.addEventListener("input", function (e) { var a = get(id);
      if (e.target.matches("input.texts")) a.texts = e.target.value;
      if (e.target.matches("input.note")) a.note = e.target.value;
      save(); paint(card); count(); });
    paint(card);
  });
  var who = document.getElementById("who");
  if (who) { who.value = state.confirmed_by || "";
    who.addEventListener("input", function () { state.confirmed_by = who.value; save(); }); }
  count();
  function entry(card) {
    var id = card.getAttribute("data-id"), type = card.getAttribute("data-type"), a = get(id);
    if (!a.v) return null;
    var e = { candidate: id, verdict: a.v };
    if (a.note && a.note.trim()) e.note = a.note.trim();
    if (a.v !== "yes") return e;
    if (type === "label-stated") {
      if (a.mistake) e.mistake = a.mistake;
      if (!a.mistake || a.mistake === "ai_misread") {
        if (a.no_label) e.no_label = true; else e.texts = [a.texts.trim()]; }
    } else if (type === "row-box" || type === "row-batch") {
      if (a.own === "no_countertop") e.no_countertop = true;
      else if (a.box === "not_among_boxes") e.not_among_boxes = true;
      else e.box = a.box;
    } else if (type === "walls") { e.left = a.left; e.right = a.right; }
    return e;
  }
  document.getElementById("copy").addEventListener("click", function () {
    var said = document.getElementById("copied");
    if (!who || !who.value.trim()) { said.textContent = "Type your name at the top first."; return; }
    var waiting = cards.filter(function (c) { return needs(c, get(c.getAttribute("data-id"))).length; });
    if (waiting.length) { said.textContent = "Finish card(s) " + waiting.map(function (c) {
      return c.getAttribute("data-num"); }).join(", ") + " first (see 'Still needed')."; return; }
    var answers = cards.map(entry).filter(function (e) { return e; });
    if (!answers.length) { said.textContent = "Nothing answered yet."; return; }
    var text = JSON.stringify({ format: META.format, confirmed_by: who.value.trim(),
      candidates_sha256: META.candidates_sha256, answers: answers }, null, 1);
    var out = document.getElementById("out"); out.value = text;
    function show() { out.style.display = "block"; out.select(); }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        said.textContent = "Copied " + answers.length + " answer(s). Save them as the confirmations file."; },
        function () { show(); said.textContent = "Select the text below and copy it."; });
    } else { show(); said.textContent = "Select the text below and copy it."; }
  });
})();
"""


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _answer_words(answer: Mapping[str, Any] | None) -> str:
    if answer is None:
        return ""
    if "texts" in answer:
        return " or ".join(str(text) for text in answer["texts"])
    if answer.get("no_label"):
        return "no label"
    if "kinds" in answer:
        return " or ".join(str(kind).replace("_", " ") for kind in answer["kinds"])
    if "box" in answer:
        return f"box {answer['box']}"
    kind = str(answer.get("kind", ""))
    return {"no_countertop": "no countertop on this page"}.get(kind, kind.replace("_", " "))


def _layout_words(layout: str) -> str:
    return WALL_LAYOUT_WORDS.get(layout, layout.replace("_", " "))


def _sentences(candidate: Candidate, kind: str) -> tuple[str, str]:
    """The reviewer's decision in one plain sentence, and what the person is asked."""
    recorded = candidate.recorded
    if kind == QuestionType.LABEL_PROPOSED:
        reading = recorded.get("reading")
        said = f" (the AI read {reading})" if reading else ""
        return (
            f"The reviewer confirmed this reading was right{said}.",
            "Is the proposed answer exactly what is printed in the picture?",
        )
    if kind == QuestionType.LABEL_STATED:
        if "corrected" in recorded:
            decision = (
                f"The reviewer corrected the value from {recorded.get('original')} to "
                f"{recorded.get('corrected')}."
            )
        elif "typed" in recorded or "inches" in recorded:
            decision = (
                f"The reviewer typed {recorded.get('typed') or recorded.get('inches')} "
                "for this width, which the readers had held."
            )
        else:
            decision = "The reviewer confirmed this reading, but it holds no text."
        ask = "Type the label exactly as it is printed in the picture (or No label)."
        if candidate.mistake_needed == MISTAKE_UNANSWERED:
            ask = (
                "Did the AI misread it, or did the vendor draw it wrong? If the AI misread it, "
                + ask[0].lower()
                + ask[1:]
            )
        return decision, ask
    if kind == QuestionType.ROW_STATED:
        return (
            "The reviewer confirmed this page has no countertop.",
            "Is there no countertop row on this page?",
        )
    if kind == QuestionType.ROW_BOX:
        return (
            (
                "The reviewer said this page HAS a countertop, where the page was listed as "
                "having none."
            ),
            "Which numbered box on the picture is the countertop row?",
        )
    if kind == QuestionType.ROW_BATCH:
        said = _answer_words({"kind": str(recorded.get("answer"))})
        return (
            f"The reviewer answered this page together with others in one click ({said}).",
            "Look at this page on its own: what is the answer here?",
        )
    if kind == QuestionType.WALLS:
        layout = next((str(v) for v in recorded.values() if isinstance(v, str)), "")
        left, right = site_ends(layout)
        return (
            (
                f"The reviewer chose the walls for the site: {_layout_words(layout)} (that is "
                f"the site answer: left end {left.replace('_', ' ')}, right end "
                f"{right.replace('_', ' ')})."
            ),
            "What did the vendor DRAW at each end of the row (not what the site has)?",
        )
    piece = recorded.get("piece")
    place = f"piece {piece + 1} from the left" if isinstance(piece, int) else "this piece"
    return (
        (
            f"The reviewer said {place} is a {str(recorded.get('kind')).replace('_', ' ')} "
            f"(the suggestion was {str(recorded.get('suggested')).replace('_', ' ')})."
        ),
        "Is that right for this piece?",
    )


def _pictures_html(pictures: Sequence[Picture]) -> tuple[str, bool]:
    if not pictures:
        return (
            (
                '<p class="problem">No picture was looked up for this case, so it cannot be '
                "answered here.</p>"
            ),
            False,
        )
    parts: list[str] = []
    usable = True
    for picture in pictures:
        if picture.png is None:
            usable = False
            parts.append(
                f'<p class="problem">Picture {_e(picture.name)}: {_e(picture.problem)}. This case '
                "cannot be answered here.</p>"
            )
            continue
        data = base64.b64encode(picture.png).decode()
        parts.append(
            f'<figure><img src="data:image/png;base64,{data}" alt="{_e(picture.name)}">'
            f"<figcaption>{_e(picture.name.replace('_', ' '))} (the picture the AI saw)"
            "</figcaption></figure>"
        )
    return f'<div class="pics">{"".join(parts)}</div>', usable


def _controls(candidate: Candidate, kind: str, boxes: int | None, prefill: str) -> str:
    rows: list[str] = []
    if kind == QuestionType.LABEL_STATED:
        if candidate.mistake_needed == MISTAKE_UNANSWERED:
            rows.append(
                '<div class="row"><span class="label">Mistake</span>'
                '<button data-mistake="ai_misread">AI misread</button>'
                '<button data-mistake="vendor_drew_it">The vendor drew it wrong</button>'
                '<button data-mistake="unsure">Not sure</button></div>'
            )
        rows.append(
            '<div class="row"><span class="label">Printed</span>'
            f'<input class="texts" data-prefill="{_e(prefill)}" '
            'placeholder="as printed, e.g. 12 1/2&quot;">'
            "<button data-nolabel>No label</button></div>"
        )
    if kind in (QuestionType.ROW_BOX, QuestionType.ROW_BATCH):
        if kind == QuestionType.ROW_BATCH and candidate.recorded.get("answer") == "no_countertop":
            rows.append(
                '<div class="row"><button data-own="no_countertop">No countertop on this page'
                "</button></div>"
            )
        else:
            count = boxes or UNKNOWN_BOXES
            buttons = "".join(f'<button data-box="{n}">{n}</button>' for n in range(1, count + 1))
            unknown = (
                ""
                if boxes
                else (
                    '<span class="muted">(how many boxes the picture numbers is not recorded)</span>'
                )
            )
            rows.append(
                f'<div class="row"><span class="label">Box</span>{buttons}'
                '<button data-box="not_among_boxes">Not among the boxes</button>'
                f"{unknown}</div>"
            )
    if kind == QuestionType.WALLS:
        for end in ("left", "right"):
            buttons = "".join(
                f'<button data-end="{end}" data-drawn="{drawn}">{_e(DRAWN_WORDS[drawn])}</button>'
                for drawn in DRAWN
            )
            rows.append(
                f'<div class="row"><span class="label">{end.title()} end</span>{buttons}</div>'
            )
    return "".join(rows)


def _card(
    number: int,
    candidate: Candidate,
    pictures: Sequence[Picture],
    boxes: int | None,
    prefill: str,
) -> str:
    kind = question_type(candidate)
    assert kind is not None and candidate.question is not None
    shown, usable = _pictures_html(pictures)
    if candidate.answer is not None:
        source = candidate.answer_source or "decision"
        proposal = (
            f'<p class="proposal">Proposed answer: <b>{_e(_answer_words(candidate.answer))}</b>'
            f'<br><span class="source">from {_e(source)}: '
            f"{_e(SOURCES.get(source, source))}</span></p>"
        )
    else:
        proposal = (
            '<p class="proposal">Proposed answer: <b>none</b><br><span class="source">you state '
            "it from the picture</span></p>"
        )
    decision, ask = _sentences(candidate, kind)
    answer = (
        f"{_controls(candidate, kind, boxes, prefill)}"
        '<div class="row verdict"><button class="yes" data-v="yes">Yes</button>'
        '<button class="no" data-v="no">No</button>'
        '<button class="unsure" data-v="unsure">Unsure</button>'
        '<input class="note" placeholder="note (optional)"></div><p class="missing"></p>'
        if usable
        else ""
    )
    mistake = "1" if candidate.mistake_needed == MISTAKE_UNANSWERED else "0"
    page = "page ?" if candidate.page_number is None else f"page {candidate.page_number}"
    question = json.dumps(dict(candidate.question), ensure_ascii=False)
    return (
        f'<section class="card" data-id="{_e(candidate.candidate_id)}" data-type="{_e(kind)}" '
        f'data-num="{number}" data-mistake-needed="{mistake}">'
        f'<div class="head"><span class="num">{number}</span>'
        f'<span class="badge">{_e(candidate.agent)}: {_e(TYPE_WORDS[kind])}</span>'
        f'<span class="muted">{_e(page)}; decided by {_e(candidate.decided_by)} on '
        f"{_e(candidate.decided_at[:10])}</span></div>"
        f"{shown}{proposal}"
        f'<p class="decision">{_e(decision)}</p><p class="ask">{_e(ask)}</p>{answer}'
        f"<details><summary>details</summary><code>{_e(candidate.candidate_id)}; decided "
        f"{_e(candidate.decided_at[:10])}; question {_e(question)}</code></details></section>"
    )


def build_page(
    export: CandidateExport,
    *,
    candidates_sha256: str,
    candidates_path: str,
    pictures: Mapping[str, Sequence[Picture]],
    boxes: Mapping[str, int] | None = None,
    prefill: Mapping[str, str] | None = None,
) -> str:
    """The review page for every ready candidate of the export, as one self-contained HTML file."""
    ready = ready_candidates(export)
    meta = json.dumps(
        {"format": CONFIRMATIONS_FORMAT, "candidates_sha256": candidates_sha256}
    ).replace("<", "\\u003c")
    cards = "".join(
        _card(
            number,
            candidate,
            pictures.get(candidate.candidate_id, ()),
            (boxes or {}).get(candidate.candidate_id),
            (prefill or {}).get(candidate.candidate_id, ""),
        )
        for number, candidate in enumerate(ready, start=1)
    )
    if not ready:
        cards = (
            '<section class="empty"><p><b>Nothing to confirm.</b> No candidate in this file is a '
            "current decision by a listed real reviewer that a key can take.</p></section>"
        )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Smriti Review</title><style>{_STYLE}</style></head><body>"
        '<div class="bar"><b>Smriti review</b><span id="count"></span>'
        '<label>Your name <input id="who" autocomplete="off"></label>'
        '<button id="copy" class="copy">Copy answers</button><span id="copied"></span></div>'
        "<main><h1>Confirm each case against the picture the AI saw</h1>"
        f'<p class="intro">{len(ready)} case(s) from {_e(export.database)} (candidates file '
        f"<code>{_e(candidates_path)}</code>, exported {_e(export.exported_at[:19])}). Only a Yes "
        "joins an answer key; No and Unsure are recorded and never join. Look at the picture "
        "first. Then press Copy answers and save the text as the confirmations file.</p>"
        f'{cards}<textarea id="out" readonly></textarea></main>'
        f'<script type="application/json" id="meta">{meta}</script>'
        f"<script>{_SCRIPT}</script></body></html>\n"
    )


# --- the command ---------------------------------------------------------------------------------


def default_out(database: str, day: str) -> Path:
    """Where the page goes when no --out is given: `~/gv-local/smriti/<date>/`."""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", database).strip("-") or "database"
    return Path.home() / "gv-local" / "smriti" / day / f"review-{safe}.html"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("candidates", type=Path, help="the candidates file (smriti_candidates)")
    parser.add_argument("--db", required=True, help="the database the candidates came from")
    parser.add_argument("--store", type=Path, required=True, help="the artifact store's root")
    parser.add_argument("--out", type=Path, help="the page (default ~/gv-local/smriti/<date>/)")
    args = parser.parse_args(argv)
    try:
        path = args.candidates.expanduser()
        export = read_candidates(path)
        sha = json.loads(path.read_text(encoding="utf-8"))["sha256"]
        day = datetime.now().astimezone().date().isoformat()  # the person's own date
        out = _outside_git(args.out if args.out is not None else default_out(export.database, day))
        if out.exists():
            raise Refused(f"{out} already exists; a page is written once (choose another --out)")
        store = args.store.expanduser().resolve()
        if not store.is_dir():
            raise Refused(f"the artifact store {store} does not exist; give the store's root")
        ready = ready_candidates(export)
        engine = create_engine(_database_url(args.db))
        try:
            with Session(engine) as session:
                try:
                    session.execute(text("SET TRANSACTION READ ONLY"))
                    pictures, boxes, prefill = find_pictures(session, store, ready)
                finally:
                    session.rollback()
        finally:
            engine.dispose()
        page = build_page(
            export,
            candidates_sha256=sha,
            candidates_path=str(path),
            pictures=pictures,
            boxes=boxes,
            prefill=prefill,
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("x", encoding="utf-8") as handle:
            handle.write(page)
    except (Refused, CandidatesFileError, FileExistsError, FileNotFoundError) as error:
        print(f"smriti review page: {error}", file=sys.stderr)
        return REFUSED
    missing = sum(
        1 for found in pictures.values() if not found or any(p.png is None for p in found)
    )
    print(
        f"Smriti review page: {len(ready)} case(s) to confirm"
        + (f", {missing} without a usable picture (shown, not answerable)" if missing else "")
        + f"; written: {out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
