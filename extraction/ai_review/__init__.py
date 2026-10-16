"""The AI page review: one sharper AI reading per combined sheet, compared by code (decision 2026-10-16).

`reader.py` renders the sheet's two halves (architect / vendor) sharp, lists the architect's
dimensions code read exactly from the PDF text, asks Opus 5.5 once for every printed vendor
dimension and, per architect dimension, the vendor's dimension of the same span, and parses the
strict JSON answer. `checks.py` is code deciding: an architect row must be one code read, the
vendor's number must parse exactly, and only exact arithmetic says same or different.
"""
