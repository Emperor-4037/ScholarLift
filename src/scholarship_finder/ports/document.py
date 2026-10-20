"""Document port contract — PDF byte extraction gateway.

Backed by PyMuPDF (AGPL, fast) or pypdf (BSD, permissive).
Selected via PDF_BACKEND env var (architecture OPEN-5).
Vision/OCR fallback routes through the LLM port when a scanned PDF is detected.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ExtractedPage:
    page_number: int           # 1-indexed
    text: str
    char_count: int = 0


@dataclass
class DocumentResult:
    pages: list[ExtractedPage] = field(default_factory=list)
    total_pages: int = 0
    is_scanned: bool = False   # True → very little text extracted; vision fallback needed
    backend_used: str = ""     # "pymupdf" | "pypdf"

    def full_text(self) -> str:
        return "\n\n".join(p.text for p in self.pages)

    def page_texts(self) -> list[str]:
        return [p.text for p in self.pages]


class DocumentPort(Protocol):
    """Narrow interface for PDF byte-to-page extraction."""

    async def extract(self, pdf_bytes: bytes, max_pages: int = 50) -> DocumentResult:
        """Extract text from *pdf_bytes*, returning per-page results.

        Raises DocumentPort errors (mapped from the error taxonomy) on failure.
        """
        ...

    def capabilities(self) -> dict[str, Any]:
        """Return capability flags.

        Expected keys:
          - ocr_capable: bool
          - max_file_bytes: int
          - backend: str
        """
        ...
