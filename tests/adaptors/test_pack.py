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
    # The two pages each contribute a "g"; the re-offset pushes page 2's row far
    # below page 1's, so the two "g" tops differ by more than a single page.
    g_tops = sorted(w.top for w in gl.words if w.text == "g")
    assert len(g_tops) == 2
    assert g_tops[1] - g_tops[0] > 1000.0


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


def test_empty_pack_yields_no_sections():
    assert split_pack([]) == []


def test_two_consecutive_empty_pages_do_not_crash():
    sections = split_pack([[], []])  # both titles "" -> merge branch over empty words
    assert all(s.title == "" for s in sections)


def test_non_consecutive_same_title_yields_separate_sections():
    pages = [_page("A", "1"), _page("B", "2"), _page("A", "3")]
    assert [s.title for s in split_pack(pages)] == ["A", "B", "A"]
