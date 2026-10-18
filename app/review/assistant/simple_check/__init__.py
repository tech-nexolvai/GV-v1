"""The simple full check (CONTRACT-SIMPLE, 2026-10-19): one compare call per vendor drawing.

Code makes a work list of every vendor drawing with its architect drawing(s) (`worklist`); one
model call per drawing compares the two by code's numbered labels and returns a fixed form
(`compare`, `prompts`, `schema`); code then verifies every pair before anything turns red
(`verify`, `blind`, `run`). It sits behind `assistant_full_check_mode = "simple"`; the roster stays
the default until a paid test shows this path does better.

Modules that draw pictures (`compare`) load `set_pack` and `dim_labels` by name, never by import:
`app/api/` must not reach pypdfium2 or OpenCV (`tests/api/test_no_heavy_work.py`).
"""
