"""Regroup a night-audit "pack" PDF into its constituent reports.

SkyTouch (and similar PMS) night-audit exports concatenate several distinct
reports (A/R Aging, Guest Ledger, Hotel Statistics, ...) into a single PDF.
Each report occupies a contiguous run of pages and repeats its title as the
top-most row of every page. This module regroups the per-page words back into
per-report sections so each report can be handed to its own parser
independently.
"""

from dataclasses import dataclass, field

from usali.adaptors.pdf import Word, cluster_rows

_PAGE_GAP = 2000.0  # vertical offset between merged pages; exceeds any real page height


@dataclass
class ReportSection:
    title: str
    words: list[Word] = field(default_factory=list)


def _page_title(page: list[Word]) -> str:
    rows = cluster_rows(page)
    if not rows:
        return ""
    top_row = min(rows, key=lambda r: min(w.top for w in r))
    return " ".join(w.text for w in sorted(top_row, key=lambda w: w.x0)).strip()


def split_pack(pages: list[list[Word]]) -> list[ReportSection]:
    sections: list[ReportSection] = []
    for page in pages:
        # Grouping relies on stable, page-invariant title rows: a per-page token
        # like "Page 1 of 3" in the top row would silently split one report into
        # per-page sections.
        title = _page_title(page)
        if sections and sections[-1].title == title:
            # Invariant: each merged page is shifted below all prior rows by a
            # constant offset. This preserves intra-page row spacing while keeping
            # cluster_rows from bridging two pages into one visual row. `default`
            # guards a section whose prior page had no words (e.g. two blank pages).
            base = max((w.top for w in sections[-1].words), default=0.0) + _PAGE_GAP
            sections[-1].words.extend(
                Word(text=w.text, x0=w.x0, top=w.top + base) for w in page
            )
        else:
            sections.append(ReportSection(title=title, words=list(page)))
    return sections
