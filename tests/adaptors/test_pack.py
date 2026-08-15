from usali.adaptors.pack import split_pack
from usali.adaptors.pdf import Word


def _page(title, *rows):
    ws = [Word(text=t, x0=75.0, top=10.0) for t in title.split()]
    for i, row in enumerate(rows, start=1):
        ws += [Word(text=t, x0=75.0 + 60 * j, top=10.0 + 15 * i) for j, t in enumerate(row.split())]
    return ws


def test_split_groups_consecutive_same_title_pages():
    pages = [
        _page("A/R Aging", "acct 1 2"),
        _page("Guest Ledger", "g 1"),
        _page("Guest Ledger", "g 2"),  # same title -> same section
        _page("Hotel Statistics", "Total Rooms 5"),
    ]
    sections = split_pack(pages)
    assert [s.title for s in sections] == ["A/R Aging", "Guest Ledger", "Hotel Statistics"]
    gl = next(s for s in sections if s.title == "Guest Ledger")
    assert any(w.text == "g" for w in gl.words)
    tops = [w.top for w in gl.words]
    assert len(set(tops)) > 1  # re-offset kept rows distinct


def test_multipage_merge_preserves_second_page_rows_and_reoffsets():
    pages = [
        _page("Guest Ledger", "alpha 1"),
        _page("Guest Ledger", "omega 2"),  # second page's distinctive row
    ]
    sections = split_pack(pages)
    assert len(sections) == 1
    gl = sections[0]
    # Second page's content survives the merge.
    assert any(w.text == "omega" for w in gl.words)
    assert any(w.text == "alpha" for w in gl.words)
    # The second page's rows are pushed below all first-page rows (monotonic top).
    first_page_max = max(w.top for w in pages[0])
    omega = next(w for w in gl.words if w.text == "omega")
    assert omega.top > first_page_max


def test_single_page_pack_yields_one_section():
    pages = [_page("Hotel Statistics", "Total Rooms 5")]
    sections = split_pack(pages)
    assert len(sections) == 1
    assert sections[0].title == "Hotel Statistics"
    assert any(w.text == "Total" for w in sections[0].words)
