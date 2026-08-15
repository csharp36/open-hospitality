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
        title = _page_title(page)
        if sections and sections[-1].title == title:
            base = max(w.top for w in sections[-1].words) + _PAGE_GAP
            sections[-1].words.extend(
                Word(text=w.text, x0=w.x0, top=w.top + base) for w in page
            )
        else:
            sections.append(ReportSection(title=title, words=list(page)))
    return sections
