"""Set pages: one page number per page of the set, across its files (#1722, separate PDFs step 3).

The simple full check names pages, not files: a label is `p23.D17`, a drawing `p23.v1` or
`p23.a1` (`schema.label_ref`, `worklist`), and the model, verify, the blind look, the outputs and a
replay all read those ids. GV's combined sheets are one file, so a page number was enough. A set
uploaded as separate files (the vendor's PDF and the architect's PDF, #1719) has a page 1 in each.

**The numbering.** The vendor's file (the work list's file) keeps its own page numbers; each separate
file whose drawings took their role from its upload (`page_map.json`'s `upload_roles`, #1719) is
numbered after it, in the page map's file order: a 5-page vendor file and a 1-page architect file
make set pages 1-5 (the vendor's) and 6 (the architect's page 1). A combined set has no other file
read so, and every set page is its own page: nothing changes for it. The numbering is fixed by the
pack, so a replay of saved answers reads the same ids.

**Where it is used.** Everywhere a PDF or a per-file fact of the pack (`dims.json`, `scales.json`,
`notes.json`, `page_texts.json`) is read for a page the check names: `own_page` turns a set page
back into the file's own page, `to_set` a file's page into its set page.

Pure: plain values in and out.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = ["SetPages", "set_pages"]


@dataclass(frozen=True, slots=True)
class SetPages:
    """The set's page numbering: the vendor's file first, then each separate file read by its
    upload, each `(file, offset, pages)` (set page = offset + the file's own page)."""

    main: str
    files: tuple[tuple[str, int, int], ...] = ()
    _offsets: Mapping[str, int] = field(default_factory=dict)

    @classmethod
    def of(cls, main: str, files: tuple[tuple[str, int, int], ...]) -> SetPages:
        return cls(main, files, {name: offset for name, offset, _count in files})

    def joined(self, file: str) -> bool:
        """Whether the file's pages are in the set's numbering (the vendor's file, or a separate
        file read by its upload)."""
        return file == self.main or file in self._offsets

    @property
    def separate(self) -> bool:
        """Whether any other file is numbered after the vendor's."""
        return any(name != self.main for name, _offset, _count in self.files)

    def to_set(self, file: str, page: int) -> int:
        """The set page of `file`'s own page `page` (the same number for the vendor's file and for
        a file not in the numbering)."""
        return page + self._offsets.get(file, 0)

    def own_page(self, file: str, page: int) -> int:
        """The file's own page of set page `page` (the same number for the vendor's file and for a
        file not in the numbering)."""
        offset = self._offsets.get(file, 0)
        return page - offset if page > offset else page

    def file_of(self, page: int) -> tuple[str, int]:
        """The file and its own page that set page `page` is."""
        for name, offset, count in self.files:
            if offset < page <= offset + count:
                return name, page - offset
        return self.main, page


def set_pages(page_map: Mapping[str, Any] | None, main_file: str | None) -> SetPages:
    """The numbering of a pack's page map, with `main_file` (the vendor's file) first."""
    document = page_map if isinstance(page_map, Mapping) else {}
    entries = [e for e in document.get("pages") or [] if isinstance(e, Mapping)]
    files = [str(name) for name in document.get("files") or []]
    for entry in entries:
        name = str(entry.get("file"))
        if name not in files:
            files.append(name)
    main = main_file if main_file is not None else (files[0] if files else "")
    counts: dict[str, int] = {}
    for entry in entries:
        page = entry.get("page")
        if isinstance(page, int):
            name = str(entry.get("file"))
            counts[name] = max(counts.get(name, 0), page)
    roles = document.get("upload_roles")
    separate = set(roles) if isinstance(roles, Mapping) else set()
    numbered: list[tuple[str, int, int]] = [(main, 0, counts.get(main, 0))]
    end = counts.get(main, 0)
    for name in files:
        if name == main or name not in separate or not counts.get(name):
            continue
        numbered.append((name, end, counts[name]))
        end += counts[name]
    return SetPages.of(main, tuple(numbered))
