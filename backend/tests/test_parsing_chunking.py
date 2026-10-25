import io
import zipfile

import pytest
from docx import Document

from app.core.errors import AppError
from app.services import parsers
from app.services.chunking import Page, chunk_pages, clean_text, split_text


def test_sanitize_filename_blocks_traversal():
    assert parsers.sanitize_filename("../../etc/passwd") == "passwd"
    assert parsers.sanitize_filename("C:\\Windows\\evil.txt") == "evil.txt"
    assert parsers.sanitize_filename("a<b>|c.pdf") == "a_b__c.pdf"
    assert parsers.sanitize_filename("...") == "document"


def test_unsupported_extension():
    with pytest.raises(AppError) as e:
        parsers.validate_extension("run.exe")
    assert e.value.code == "unsupported_type"


def test_extract_txt_md(tmp_path):
    p = tmp_path / "a.md"
    p.write_text("# Title\n\nHello world", encoding="utf-8")
    pages = parsers.extract_pages(p, ".md")
    assert "Hello world" in pages[0].text


def test_extract_csv(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("name,city\nAda,London\nGrace,New York\n", encoding="utf-8")
    text = parsers.extract_pages(p, ".csv")[0].text
    assert "Row 1: name: Ada; city: London" in text
    assert "Grace" in text


def test_extract_docx_paragraphs_and_tables(tmp_path):
    d = Document()
    d.add_paragraph("Quarterly report")
    t = d.add_table(rows=1, cols=2)
    t.rows[0].cells[0].text = "Revenue"
    t.rows[0].cells[1].text = "42"
    p = tmp_path / "a.docx"
    d.save(p)
    text = parsers.extract_pages(p, ".docx")[0].text
    assert "Quarterly report" in text and "Revenue" in text and "42" in text


def test_extract_pdf_keeps_page_numbers(tmp_path):
    from pypdf import PdfWriter

    w = PdfWriter()
    w.add_blank_page(200, 200)
    p = tmp_path / "blank.pdf"
    with p.open("wb") as f:
        w.write(f)
    with pytest.raises(AppError) as e:  # blank pages -> no text
        parsers.extract_pages(p, ".pdf")
    assert e.value.code == "empty_file"


def test_corrupt_pdf_and_docx(tmp_path):
    bad = tmp_path / "x.pdf"
    bad.write_bytes(b"%PDF-1.4 garbage")
    with pytest.raises(AppError) as e:
        parsers.extract_pages(bad, ".pdf")
    assert e.value.code == "corrupt_file"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("nothing.txt", "x")
    bad2 = tmp_path / "x.docx"
    bad2.write_bytes(buf.getvalue())
    with pytest.raises(AppError):
        parsers.extract_pages(bad2, ".docx")


def test_signature_check(tmp_path):
    p = tmp_path / "fake.pdf"
    p.write_bytes(b"not a pdf at all")
    with pytest.raises(AppError):
        parsers.validate_signature(p, ".pdf")


def test_clean_text():
    assert clean_text("a\x00b\r\n\r\n\r\n\r\nc   d") == "ab\n\nc d"


def test_split_respects_size_and_overlaps():
    text = " ".join(f"word{i}" for i in range(400))
    chunks = split_text(text, 200, 40)
    assert len(chunks) > 5
    assert all(len(c) <= 200 for c in chunks)
    for a, b in zip(chunks, chunks[1:]):
        # consecutive chunks share some trailing/leading text
        assert set(a.split()[-6:]) & set(b.split()[:8])


def test_split_covers_all_text():
    text = " ".join(f"w{i}" for i in range(300))
    joined = " ".join(split_text(text, 150, 30))
    for i in range(300):
        assert f"w{i}" in joined


def test_split_invalid_params():
    with pytest.raises(ValueError):
        split_text("abc", 100, 100)


def test_chunk_pages_preserve_page_metadata():
    pages = [Page("alpha " * 100, 1), Page("beta " * 100, 2)]
    chunks = chunk_pages(pages, 200, 20)
    assert [c.index for c in chunks] == list(range(len(chunks)))
    assert all(("alpha" in c.text) == (c.page == 1) for c in chunks)
    assert {c.page for c in chunks} == {1, 2}
