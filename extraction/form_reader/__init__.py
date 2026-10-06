"""Form-first, two-reader measurement proposals.

The module deliberately separates parsed values from display placement. A reader's text may
contribute to a value after exact independent agreement; its box can only describe where a
reviewer should look.
"""

from extraction.form_reader.agreement import ComparedReading, compare_page_answers
from extraction.form_reader.locator import LocatedBox, locate_box
from extraction.form_reader.parser import ParsedDimension, parse_dimension
from extraction.form_reader.schema import CountertopForm, FormDimension, PageFormAnswer

__all__ = [
    "ComparedReading",
    "CountertopForm",
    "FormDimension",
    "LocatedBox",
    "PageFormAnswer",
    "ParsedDimension",
    "compare_page_answers",
    "locate_box",
    "parse_dimension",
]
