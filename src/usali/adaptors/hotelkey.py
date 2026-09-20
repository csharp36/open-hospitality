"""Header facts shared by every HotelKey report, PDF or XLSX.

Each report prints ``Date: Mon D, YYYY`` (one day) or ``Date Range: Mon D, YYYY
- Mon D, YYYY`` in its header block, and every one also stamps ``Report Run
Date:``. The run date is the morning after the business date, so matching
``Date:`` without excluding ``Run`` returns the wrong day; pinned in
tests/adaptors/test_hotelkey_dates.py by
test_pdf_header_date_is_the_report_date_not_the_run_date.

A range yields its END date (test_xlsx_date_range_yields_the_range_end): the
settlement export can span two days, and this function answers "which day
does the export close on"; a caller that needs per-row dates reads them from
the rows themselves.

The vendor prints ``Date:`` capitalized and three-letter month abbreviations
(``Aug 13, 2026``) in every sample read on 2026-09-20; a header that breaks
either (lowercase, ``Sept``) raises rather than guessing
(test_only_a_run_date_is_not_a_business_date is the refusal path).
"""

import re
from datetime import date

from usali.adaptors.pdf import Word
from usali.normalize import parse_hotelkey_date

_HEADER_WORD_LIMIT = 60
_MONTH_DATE = r"[A-Z][a-z]{2} \d{1,2},? \d{4}"
_DATE_RE = re.compile(
    rf"(?<!Run )Date(?: Range)?: ({_MONTH_DATE})(?: - ({_MONTH_DATE}))?"
)


def extract_business_date(words: list[Word]) -> date:
    header = " ".join(w.text for w in words[:_HEADER_WORD_LIMIT])
    m = _DATE_RE.search(header)
    if m is None:
        raise ValueError("no HotelKey 'Date:' or 'Date Range:' in the report header")
    return parse_hotelkey_date(m.group(2) or m.group(1))
