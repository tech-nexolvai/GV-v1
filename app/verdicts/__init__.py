"""Writing what the engine decided into the database.

`verdict/` decides and `app/` stores, and the two must not meet in the middle: `docs/DESIGN_PLATFORM.md`
puts it as *"`verdict/` gains no persistence. Storing a finding is not deciding one"*, and
`tests/test_verdict_isolation.py` enforces it by walking imports. So the translation lives here, on the
`app/` side of that line, in the same shape `app/models/parameters.py` uses for parameter sets.
"""
