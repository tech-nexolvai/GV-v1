"""Typed errors shared by exact-arithmetic unit policies."""


class UnknownRoundingError(ValueError):
    """The rounding quantum cannot be derived because the authored token is absent."""
