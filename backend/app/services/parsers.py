"""File-type validation, filename sanitising and text extraction."""
import csv
import re
import zipfile
from pathlib import Path

from app.core.errors import AppError
from app.services.chunking import Page

ALLOWED_EXTENSIONS = {".pdf", ".txt", ".md", ".markdown", ".docx", ".csv"}
MAX_DOCX_UNCOMPRESSED = 300 * 1024 * 1024
CSV_ROWS_PER_PAGE = 100


def sanitize_filename(name: str) -> str:
    """Drop any directory part and unsafe characters; never returns an empty name."""
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f]", "", name)
    name = re.sub(r"[^\w.\- ()\[\]]", "_", name, flags=re.UNICODE).strip(" .")
    name = name[-150:]
    return name or "document"


def validate_extension(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise AppError(
            "unsupported_type",
            f"Unsupported file type '{ext or 'none'}'. Supported: PDF, TXT, MD, DOCX, CSV.",
            415,
        )
    return ext


def validate_signature(path: Path, ext: str) -> None:
    """Cheap magic-number check so a renamed binary is rejected early."""
    with path.open("rb") as f:
        head = f.read(8)
    if ext == ".pdf" and not head.startswith(b"%PDF"):
        raise AppError("corrupt_file", "File is not a valid PDF.", 422)
    if ext == ".docx" and not head.startswith(b"PK"):
        raise AppError("corrupt_file", "File is not a valid DOCX.", 422)
    if ext in {".txt", ".md", ".markdown", ".csv"} and b"\x00" in head:
        raise AppError("corrupt_file", "File looks binary, not text.", 422)


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _llama_documents(path: Path):
    """Load a file with LlamaIndex's readers (PDF -> one Document per page)."""
    from llama_index.core import SimpleDirectoryReader

    return SimpleDirectoryReader(input_files=[str(path)], raise_on_error=True).load_data()


def _pdf(path: Path) -> list[Page]:
    docs = _llama_documents(path)
    pages = []
    for i, d in enumerate(docs, start=1):
        label = str(d.metadata.get("page_label", i))
        pages.append(Page(d.get_content(), int(label) if label.isdigit() else i))
    return pages


def _docx(path: Path) -> list[Page]:
    with zipfile.ZipFile(path) as z:
        if sum(i.file_size for i in z.infolist()) > MAX_DOCX_UNCOMPRESSED:
            raise AppError("corrupt_file", "DOCX expands to an unsafe size.", 422)
    return [Page("\n\n".join(d.get_content() for d in _llama_documents(path)), None)]


def _markdown(path: Path) -> list[Page]:
    return [Page("\n\n".join(d.get_content() for d in _llama_documents(path)), None)]


def _csv(path: Path) -> list[Page]:
    csv.field_size_limit(10_000_000)
    text = _read_text(path)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = csv.reader(text.splitlines(), dialect)
    header = next(rows, None)
    if not header:
        return []
    pages: list[Page] = []
    lines: list[str] = []
    for n, row in enumerate(rows, start=1):
        if not any(c.strip() for c in row):
            continue
        fields = "; ".join(f"{h.strip()}: {v.strip()}" for h, v in zip(header, row) if v.strip())
        lines.append(f"Row {n}: {fields}")
        if len(lines) >= CSV_ROWS_PER_PAGE:
            pages.append(Page("\n".join(lines), None))
            lines = []
    if lines:
        pages.append(Page("\n".join(lines), None))
    return pages


def extract_pages(path: Path, ext: str) -> list[Page]:
    """Blocking; call via asyncio.to_thread. Raises AppError on unusable files."""
    try:
        if ext == ".pdf":
            pages = _pdf(path)
        elif ext == ".docx":
            pages = _docx(path)
        elif ext == ".csv":
            pages = _csv(path)
        elif ext in (".md", ".markdown"):
            pages = _markdown(path)
        else:
            pages = [Page(_read_text(path), None)]
    except AppError:
        raise
    except Exception as exc:  # parser libraries raise many unrelated types
        raise AppError("corrupt_file", f"Could not read file: {type(exc).__name__}: {exc}", 422) from exc
    if not any(p.text.strip() for p in pages):
        raise AppError(
            "empty_file",
            "No extractable text found (empty file, or a scanned PDF that needs OCR).",
            422,
        )
    return pages
