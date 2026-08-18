"""The reviewer's half of the system: sessions, actions, the correction ledger and exceptions.

The golden rule ends "a reviewer signs off", and this is where that happens. Everything here records
a human decision, which is why so much of it is append-only: a correction that can be edited is a
correction nobody can audit, and an exception that can be quietly widened is not an exception any
more, it is a rule change with no author.

Source: `docs/DESIGN_PRODUCT.md` §4
"""

from __future__ import annotations
