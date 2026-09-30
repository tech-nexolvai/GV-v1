"""Shared, lightweight configuration for the human-operated reading path."""

from typing import Final

# Vendor dual-notation dimensions measured only 8–13 pixels high at 150 DPI on the reviewed
# Board Room 1 sheet. At 300 DPI the production OCR route detects four of the five full-page
# labels while the existing page-pixel ceiling still refuses oversized allocations.
READER_RASTER_DPI: Final = 300

__all__ = ["READER_RASTER_DPI"]
