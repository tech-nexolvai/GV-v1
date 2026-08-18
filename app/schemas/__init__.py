"""Pydantic contracts for the HTTP surface.

Kept apart from `app/models/` on purpose. A SQLAlchemy model is what we store; a schema is what we
promise a client. Returning ORM rows directly would make every column rename a breaking API change,
and would eventually leak a column somebody added for internal bookkeeping.
"""
