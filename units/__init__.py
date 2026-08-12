"""Exact-arithmetic primitives shared by extraction, evidence and the verdict engine.

**This package must import the Python standard library and nothing else.**

That constraint is the reason `verdict/` is allowed to depend on it: a package that cannot
reach a network, a model or a database cannot become a route into the decision path. Add one
third-party import here and the verdict service's isolation is silently gone —
`tests/test_verdict_isolation.py` fails the build if that happens.

See ADR-0002 and `docs/DESIGN.md` §1–§2.
"""
