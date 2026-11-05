"""A neutral word for what an answer is about, so the exam can be read without client text (harness rebuild, Phase 0).

An answer's own `item` text names the client's drawing: item tags, finish codes, sink models, room
numbers. None of that may reach GitHub (`CLIENT_FACTS.md`), and none of it is needed to score a
run, so the exam stores a label from the short list below instead, chosen by the first rule that
matches. The label is a domain word ("cut-out size", "toe kick"), never a value and never a code.

A label is a convenience for reading a score by kind, not an identity: the finding id and the page
are what join an exam case to a run.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["LABELS", "label_of"]

#: Tried in order; the first whose pattern is found in the lower-cased item text wins.
LABELS: Final[tuple[tuple[str, str], ...]] = (
    (r"cut-?out|cutout", "cut-out"),
    (r"who supplies|supplier|supply|by gv|by others", "supplier note"),
    (r"title ?block|item (number|no\.?|cell)|overall size cell", "title block"),
    (r"view title|section [a-e]\b|two views|misspel|typo", "view title"),
    (r"material table|swatch|legend|not in the table", "material table"),
    (r"toe ?kick", "toe kick"),
    (r"splash", "splash"),
    (r"filler|scribe", "filler"),
    (r"faucet", "faucet"),
    (r"sink (centre|center|position|offset|model|tag|size)|sink and", "sink detail"),
    (r"\bshelf|shelves|hook rail|cup hook", "shelf"),
    (r"top (length|width)|counter(top)? length", "top length"),
    (r"top depth|depth", "depth"),
    (r"thickness", "thickness"),
    (r"overall (height|length|width)|wall[- ]to[- ]wall|run from", "overall size"),
    (r"counter height|height \(aff\)|aff\b", "height above floor"),
    (r"base cabinet|upper|wall cabinet|drawer|door|carcass|bay", "cabinet piece"),
    (
        r"appliance|fridge|refrigerator|cooktop|range|dishwasher|microwave|disposal|hood",
        "appliance",
    ),
    (r"finish|grain|colour|color|tag", "finish tag"),
    (r"hardware|pull|hinge|leg|bracket|glide", "hardware"),
    (r"quantity|qty|room list|rooms|count|matrix|kit", "quantity or room list"),
    (r"layout|arrangement|hand\b|mirror|order of", "layout"),
    (r"light|puck|led|outlet|j-?box|grommet", "lighting or power"),
    (r"string|sum|adds up|parts|dimension", "dimension string"),
    (r"edge|ease|profile|chamfer|mitre|miter|joint|seam", "edge or joint"),
    (r"date|revision|issue", "date or revision"),
)


def label_of(item: str) -> str:
    """The exam's neutral label for an answer's item text, or "other" when nothing matches."""
    text = (item or "").lower()
    for pattern, label in LABELS:
        if re.search(pattern, text):
            return label
    return "other"
