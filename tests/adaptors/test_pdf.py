from usali.adaptors.pdf import extract_pages


def test_extract_pages_groups_by_page():
    pages = extract_pages("docs/reference/samples/SkyTouch - Standard Audit Pack (mock).pdf")
    assert len(pages) >= 3
    assert all(isinstance(p, list) and p for p in pages)
    assert pages[0][0].text  # first page has words
