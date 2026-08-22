"""Content kinds that may enter semantic retrieval.

There is deliberately no identifier member. Codes are handled by exact, alias, trigram and lexical
lanes; making them unrepresentable here is stronger than asking callers to remember a warning.
"""

from enum import StrEnum


class DenseContentKind(StrEnum):
    """Prose-like drawing content eligible for dense retrieval."""

    NOTE = "note"
    MATERIAL_DESCRIPTION = "material_description"
    VIEW_TITLE = "view_title"
