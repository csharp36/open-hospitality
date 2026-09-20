import pytest

from usali.adaptors.reader import ACCEPTED_FORMATS, is_pdf, is_xlsx, read_words_from_bytes


def test_magic_bytes_decide_the_format_not_the_suffix():
    assert is_pdf(b"%PDF-1.7 ...") and not is_xlsx(b"%PDF-1.7 ...")
    assert is_xlsx(b"PK\x03\x04...") and not is_pdf(b"PK\x03\x04...")
    assert not is_pdf(b"") and not is_xlsx(b"")


def test_unknown_bytes_are_refused_loudly():
    with pytest.raises(ValueError, match=ACCEPTED_FORMATS):
        read_words_from_bytes(b"hello, not a report")


def test_a_zip_that_is_not_a_workbook_is_refused_loudly(tmp_path):
    import zipfile
    p = tmp_path / "bogus.xlsx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("hello.txt", "not a workbook")
    with pytest.raises(ValueError, match="not an XLSX workbook"):
        read_words_from_bytes(p.read_bytes())
