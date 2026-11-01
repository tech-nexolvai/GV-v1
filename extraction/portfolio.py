"""PDF portfolios at the front door: each attached PDF becomes a document of its own (#1750, item 2).

**Why.** A PDF portfolio (a file whose catalog has a `/Collection`, its files in the
`/EmbeddedFiles` name tree) shows a cover page ("open this in Acrobat") while the real drawings
are files attached to it. Read as an ordinary PDF, only the cover is read and the drawings are
never seen. So before the front door (`extraction/working_copy.py`), an upload is looked at once:

- **A portfolio** (a `/Collection`, or a file of one page with PDF files attached to it): each
  attached PDF is taken out and sent through the front door as its own document, named after the
  attachment's file name, exactly as if it had been uploaded. A `/Collection`'s cover is not read;
  the one page of a file that is not a `/Collection` is kept as its own document too (it may be a
  drawing, and a page that is only a cover is skipped later like any title page).
- **Any other attached file** is not read, and the reviewer is told which and why in plain words:
  a spreadsheet, a picture, a CAD file, a PDF attached to a drawing set (not a portfolio), a
  portfolio inside the portfolio (one level is opened, never more), a file too big, a damaged one.
- **An ordinary PDF** with nothing attached gets `None` here and goes on byte for byte.

**Guards.** At most `MAX_ATTACHMENTS` attached files (more: the portfolio is refused, plainly), at
most `MAX_ATTACHMENT_BYTES` each and `MAX_TOTAL_BYTES` in all (counted while unpacking, so a
small file that unpacks into a huge one is stopped at the limit, never held whole). Nothing in the
file is ever run: attachments are only read as bytes; a file whose name says it is not a PDF is
not even unpacked; only PDFs (by their first bytes, never by their names) go on; nothing that is
not a PDF is written anywhere.

Source: issue #1750 item 2 · Verification: `tests/extraction/test_portfolio.py`
"""

from __future__ import annotations

import io
import zlib
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Final

import pikepdf

__all__ = [
    "MAX_ATTACHMENTS",
    "MAX_ATTACHMENT_BYTES",
    "MAX_TOTAL_BYTES",
    "Part",
    "Skipped",
    "Unpacked",
    "unpack",
]

#: The most attached files a portfolio may hold; one with more is not unpacked at all.
MAX_ATTACHMENTS: Final = 40
#: The most one attached file may be once unpacked, and all of them together.
MAX_ATTACHMENT_BYTES: Final = 300_000_000
MAX_TOTAL_BYTES: Final = 600_000_000

#: A PDF starts with this within its first kilobyte (PDF readers allow a little junk before it).
_PDF_MAGIC: Final = b"%PDF-"
_MAGIC_WITHIN: Final = 1024

#: What an attached file that is not a PDF is, by its name's ending, in plain words.
_KINDS: Final[dict[str, str]] = {
    **dict.fromkeys(
        (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".gif", ".bmp", ".heic", ".webp"), "a picture"
    ),
    **dict.fromkeys((".xlsx", ".xlsm", ".xls", ".csv", ".ods", ".numbers"), "a spreadsheet"),
    **dict.fromkeys((".doc", ".docx", ".txt", ".rtf", ".odt", ".pages"), "a text document"),
    **dict.fromkeys((".dwg", ".dxf", ".rvt", ".skp", ".dwf", ".ifc"), "a CAD file"),
    **dict.fromkeys((".zip", ".7z", ".rar"), "a compressed folder"),
}


@dataclass(frozen=True, slots=True)
class Part:
    """One attached PDF, to go through the front door as its own document."""

    name: str
    """The attachment's file name (no folder)."""
    data: bytes
    pages: int


@dataclass(frozen=True, slots=True)
class Skipped:
    """One attached file that is not read, and why, in plain words."""

    name: str
    why: str


@dataclass(frozen=True, slots=True)
class Unpacked:
    """What an upload with files attached to it holds."""

    portfolio: bool
    """Its attached PDFs are read as documents of their own (`parts`)."""
    keep_own_pages: bool
    """Its own pages are read too: always for a file that is not a portfolio, and for a
    portfolio that is not a `/Collection` (its one page may be a drawing)."""
    parts: tuple[Part, ...] = ()
    skipped: tuple[Skipped, ...] = ()
    refused: str | None = None
    """Why the portfolio was not unpacked at all (too many attached files, or no PDF in it that
    could be read), in plain words; `None` when it was."""


class _NotRead(ValueError):
    """One attached file is not read; the message is the plain reason. `inner`: the files attached
    to it in turn (a portfolio inside the portfolio), each not read either."""

    def __init__(self, why: str, inner: tuple[Skipped, ...] = ()) -> None:
        super().__init__(why)
        self.inner = inner


def unpack(
    data: bytes,
    *,
    max_attachments: int | None = None,
    max_bytes: int | None = None,
    max_total: int | None = None,
) -> Unpacked | None:
    """What the upload `data` has attached to it (see the module docstring), or `None` when it has
    nothing attached or cannot be opened (the front door then says why, as before). The limits
    default to the module's (`MAX_ATTACHMENTS`, `MAX_ATTACHMENT_BYTES`, `MAX_TOTAL_BYTES`)."""
    most = MAX_ATTACHMENTS if max_attachments is None else max_attachments
    each = MAX_ATTACHMENT_BYTES if max_bytes is None else max_bytes
    total = MAX_TOTAL_BYTES if max_total is None else max_total
    try:
        pdf = pikepdf.open(io.BytesIO(data))
    except (pikepdf.PasswordError, pikepdf.PdfError):
        return None
    with pdf:
        attached = _attached(pdf)
        if not attached:
            return None
        portfolio = _is_portfolio(pdf, attached)
        keep = "/Collection" not in pdf.Root
        if not portfolio:
            return Unpacked(
                portfolio=False,
                keep_own_pages=True,
                skipped=tuple(
                    Skipped(
                        name,
                        _not_pdf_words(name)
                        or "a file attached to a drawing set: attachments of an ordinary PDF are "
                        "not read",
                    )
                    for name, _spec in attached
                ),
            )
        if len(attached) > most:
            return Unpacked(
                portfolio=True,
                keep_own_pages=keep,
                refused=f"this PDF portfolio holds {len(attached)} attached files, more than the "
                f"{most} that are unpacked; upload its drawings as separate PDFs",
            )
        parts: list[Part] = []
        skipped: list[Skipped] = []
        left = total
        for name, spec in attached:
            big = f"too big (the most one attached file may be is {_mb(each)})"
            if left < each:
                big = f"too big (the attached files together may be at most {_mb(total)})"
            try:
                if not _says_pdf(name, spec) and (kind := _not_pdf_words(name)):
                    raise _NotRead(kind)
                raw = _contents(spec, min(each, left), big)
                part, inner = _part(name, raw)
            except _NotRead as why:
                skipped.append(Skipped(name, str(why)))
                skipped.extend(why.inner)
                continue
            parts.append(part)
            skipped.extend(inner)
            left -= len(raw)
    refused = (
        None
        if parts or keep
        else "this PDF portfolio holds no drawing PDF that could be read (see the attached files "
        "listed as not read)"
    )
    return Unpacked(
        portfolio=True,
        keep_own_pages=keep,
        parts=tuple(parts),
        skipped=tuple(skipped),
        refused=refused,
    )


# ---- the attached files -------------------------------------------------------------------------


def _attached(pdf: pikepdf.Pdf) -> list[tuple[str, Any]]:
    """The files in the `/EmbeddedFiles` name tree, in its order: (file name, file spec)."""
    try:
        items = list(pdf.attachments.items())
    except (pikepdf.PdfError, TypeError, ValueError):
        return []
    out: list[tuple[str, Any]] = []
    for key, spec in items:
        try:
            raw = spec.filename or key
        except (pikepdf.PdfError, TypeError, ValueError):
            raw = key
        out.append((_base_name(str(raw or key)), spec))
    return out


def _is_portfolio(pdf: pikepdf.Pdf, attached: list[tuple[str, Any]]) -> bool:
    """A `/Collection`, or a file of one page with a PDF attached to it (its page only a cover)."""
    if not attached:
        return False
    if "/Collection" in pdf.Root:
        return True
    return len(pdf.pages) <= 1 and any(_says_pdf(name, spec) for name, spec in attached)


def _base_name(name: str) -> str:
    """The file name with any folder (either slash) left off."""
    return PureWindowsPath(PurePosixPath(name).name).name or "attachment"


def _suffix(name: str) -> str:
    return PurePosixPath(name.lower()).suffix


def _says_pdf(name: str, spec: Any) -> bool:
    """The attachment says it is a PDF (its name or its declared type); the bytes decide later."""
    if _suffix(name) == ".pdf":
        return True
    try:
        subtype = spec.obj.get("/EF", {}).get("/F", {}).get("/Subtype")
    except (AttributeError, pikepdf.PdfError, TypeError, ValueError):
        return False
    return str(subtype or "").lower() == "/application#2fpdf" or str(subtype) == "/application/pdf"


def _not_pdf_words(name: str) -> str | None:
    """Why a file named `name` is not read, when its name says it is not a PDF."""
    kind = _KINDS.get(_suffix(name))
    return None if kind is None else f"{kind}, not a drawing PDF"


def _contents(spec: Any, limit: int, big: str) -> bytes:
    """The attached file's bytes, never more than `limit` of them held. Raises `_NotRead` (`big`
    when it is over the limit)."""
    try:
        stream = spec.obj["/EF"]["/F"]
    except (KeyError, TypeError, pikepdf.PdfError) as error:
        raise _NotRead("it holds no file") from error
    if not isinstance(stream, pikepdf.Stream):
        raise _NotRead("it holds no file")
    try:
        declared = int(stream.get("/Params", {}).get("/Size", 0) or 0)
    except (TypeError, ValueError, pikepdf.PdfError):
        declared = 0
    if declared > limit:
        raise _NotRead(big)
    filters = stream.get("/Filter")
    names = (
        []
        if filters is None
        else [str(f) for f in filters] if isinstance(filters, pikepdf.Array) else [str(filters)]
    )
    try:
        raw = stream.read_raw_bytes()
    except pikepdf.PdfError as error:
        raise _NotRead("it is damaged and could not be unpacked") from error
    if not names:
        data = raw
    elif names == ["/FlateDecode"] and stream.get("/DecodeParms") is None:
        # Unpacked with a ceiling: a small file that would unpack into a huge one stops here.
        try:
            data = zlib.decompressobj().decompress(raw, limit + 1)
        except zlib.error as error:
            raise _NotRead("it is damaged and could not be unpacked") from error
    else:
        raise _NotRead("it is stored in a way that cannot be unpacked safely")
    if len(data) > limit:
        raise _NotRead(big)
    return data


def _mb(size: int) -> str:
    return f"{size / 1_000_000:g} MB"


def _part(name: str, data: bytes) -> tuple[Part, list[Skipped]]:
    """The attached file as a document to read, and the files attached to it in turn (one level
    deeper: listed, not read). Raises `_NotRead` when it is not read itself."""
    if _PDF_MAGIC not in data[: _MAGIC_WITHIN + len(_PDF_MAGIC)]:
        words = _not_pdf_words(name)
        if words is None:
            words = (
                "named as a PDF but it is not one"
                if _suffix(name) == ".pdf"
                else "not a PDF, so not a drawing that can be read"
            )
        raise _NotRead(words)
    try:
        pdf = pikepdf.open(io.BytesIO(data))
    except pikepdf.PasswordError as error:
        raise _NotRead("it is locked with a password, so it could not be read") from error
    except pikepdf.PdfError as error:
        raise _NotRead("it is damaged and could not be opened") from error
    with pdf:
        inner = [n for n, _spec in _attached(pdf)]
        pages = len(pdf.pages)
        if inner and _is_portfolio(pdf, _attached(pdf)):
            raise _NotRead(
                "a portfolio inside the portfolio: not opened (only one level is unpacked); "
                "upload its drawings as separate PDFs",
                tuple(
                    Skipped(n, f"inside “{name}”, a portfolio inside the portfolio") for n in inner
                ),
            )
        if pages <= 0:
            raise _NotRead("it has no pages")
    deeper = [Skipped(n, f"attached inside “{name}”: only one level is unpacked") for n in inner]
    return Part(name, data, pages), deeper
