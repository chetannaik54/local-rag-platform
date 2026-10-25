"""Text cleaning and overlapping chunking that preserves page numbers."""
import re
from dataclasses import dataclass

from langchain_text_splitters import RecursiveCharacterTextSplitter


@dataclass
class Page:
    text: str
    page: int | None = None


@dataclass
class Chunk:
    text: str
    page: int | None
    index: int



def clean_text(text: str) -> str:
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_text(text: str, size: int, overlap: int) -> list[str]:
    """Split on the most natural boundary available, with `overlap` chars shared."""
    if size <= 0 or not 0 <= overlap < size:
        raise ValueError("require size > 0 and 0 <= overlap < size")
    text = text.strip()
    if not text:
        return []
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=size, chunk_overlap=overlap,
        separators=["\n\n", "\n", ". ", " ", ""],
    )
    return [c.strip() for c in splitter.split_text(text) if c.strip()]


SEGMENT_CHARS = 100_000


def _segments(text: str, limit: int = SEGMENT_CHARS):
    """Yield slices of at most `limit` chars, cut at a line/word boundary where possible.

    Cleaning and splitting a huge string in one regex call holds the interpreter lock for seconds
    and stalls the API; short calls let other threads (the web server) run in between.
    """
    pos, n = 0, len(text)
    while pos < n:
        end = min(pos + limit, n)
        if end < n:
            cut = max(text.rfind("\n", pos, end), text.rfind(" ", pos, end))
            if cut > pos + limit // 2:
                end = cut + 1
        yield text[pos:end]
        pos = end


def chunk_pages(pages: list[Page], size: int, overlap: int) -> list[Chunk]:
    chunks: list[Chunk] = []
    for p in pages:
        for segment in _segments(p.text):
            for piece in split_text(clean_text(segment), size, overlap):
                chunks.append(Chunk(text=piece, page=p.page, index=len(chunks)))
    return chunks
