"""Phase 4 of "code finds the slots, AI reads them": slot reads (#987).

Code finds the row, its slots and each slot's whole label (`runs`); code cuts one padded crop per
label; two readers of different makers read the crop (or the file's own text and one reader, for a
label that is real text); a value seals only on identical text and is parsed from that text alone
(`seal`); a piece's kind comes from a printed word or a wall end, else it is unknown (`kinds`); and
sealed readings are offered to the reviewer's form only when the whole chain is sealed and named
(`mapping`). Every guard here only ever withholds.

Nothing in this package imports `verdict/`, and `verdict/` must never import it.
"""
