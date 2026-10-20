"""PDF document adapter — PyMuPDF (primary) + pypdf (fallback).

Runs blocking I/O in a thread pool. Detects scanned PDFs (char_count < 100
per page) and marks the DocumentResult.is_scanned flag for potential
vision-tier handling.

Backend selection via PDF_BACKEND env var: 'pymupdf' (default) or 'pypdf'.
"""
from __future__ import annotations

import asyncio
import io
import logging
from enum import Enum
from typing import Any, Final

from scholarship_finder.ports.document import DocumentPort, DocumentResult, ExtractedPage
from scholarship_finder.config import get_settings

logger = logging.getLogger(__name__)

# Threshold below which a page is considered likely scanned / image-only
_SCANNED_CHAR_THRESHOLD: Final[int] = 100
# Absolute ceiling — no single PDF should OOM the process
_MAX_BYTES: Final[int] = 50 * 1024 * 1024  # 50 MB


class _PDFBackend(str, Enum):
    PYMUPDF = "pymupdf"
    PYPDF = "pypdf"


# ---------------------------------------------------------------------------
# PyMuPDF implementation
# ---------------------------------------------------------------------------

def _extract_pymupdf(data: bytes, max_pages: int) -> list[ExtractedPage]:
    """Extract pages using PyMuPDF (fitz). Runs synchronously."""
    try:
        import fitz  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            "PyMuPDF (fitz) is not installed. Install with: pip install pymupdf"
        ) from exc

    pages: list[ExtractedPage] = []
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        limit = min(len(doc), max_pages)
        for page_num in range(limit):
            page = doc[page_num]
            text = page.get_text("text")
            char_count = len(text.strip())
            pages.append(
                ExtractedPage(
                    page_number=page_num + 1,
                    text=text,
                    char_count=char_count,
                )
            )
    finally:
        doc.close()

    return pages


# ---------------------------------------------------------------------------
# pypdf implementation
# ---------------------------------------------------------------------------

def _extract_pypdf(data: bytes, max_pages: int) -> list[ExtractedPage]:
    """Extract pages using pypdf. Runs synchronously."""
    try:
        from pypdf import PdfReader  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            "pypdf is not installed. Install with: pip install pypdf"
        ) from exc

    pages: list[ExtractedPage] = []
    reader = PdfReader(io.BytesIO(data))
    limit = min(len(reader.pages), max_pages)
    for page_num in range(limit):
        text = reader.pages[page_num].extract_text() or ""
        char_count = len(text.strip())
        pages.append(
            ExtractedPage(
                page_number=page_num + 1,
                text=text,
                char_count=char_count,
            )
        )

    return pages


# ---------------------------------------------------------------------------
# Primary adapter
# ---------------------------------------------------------------------------

class PDFDocumentAdapter(DocumentPort):
    """Extracts pages from PDF bytes using the configured backend.

    Falls back to the secondary backend automatically if the primary raises.
    Implements the DocumentPort protocol.
    """

    def __init__(self, backend: str | None = None) -> None:
        cfg = get_settings()
        raw = backend or getattr(cfg, "pdf_backend", "pymupdf")
        self._backend = _PDFBackend(raw.lower())
        logger.debug("PDFDocumentAdapter using backend=%s", self._backend)

    # ------------------------------------------------------------------
    # DocumentPort interface
    # ------------------------------------------------------------------

    async def extract(self, pdf_bytes: bytes, max_pages: int = 50) -> DocumentResult:
        """Extract text pages from PDF bytes asynchronously."""
        if not pdf_bytes:
            return DocumentResult(pages=[], total_pages=0, backend_used=self._backend.value)

        if len(pdf_bytes) > _MAX_BYTES:
            logger.warning("PDF too large (%d bytes), truncating not supported — aborting", len(pdf_bytes))
            return DocumentResult(pages=[], total_pages=0, backend_used=self._backend.value)

        loop = asyncio.get_running_loop()

        if self._backend == _PDFBackend.PYMUPDF:
            primary_fn = _extract_pymupdf
            fallback_fn = _extract_pypdf
            fallback_name = "pypdf"
        else:
            primary_fn = _extract_pypdf
            fallback_fn = _extract_pymupdf
            fallback_name = "pymupdf"

        pages: list[ExtractedPage] = []
        backend_used = self._backend.value

        try:
            pages = await loop.run_in_executor(None, primary_fn, pdf_bytes, max_pages)
        except Exception as primary_exc:
            logger.warning(
                "Primary PDF backend %s failed (%s), falling back to %s",
                self._backend.value,
                primary_exc,
                fallback_name,
            )
            try:
                pages = await loop.run_in_executor(None, fallback_fn, pdf_bytes, max_pages)
                backend_used = fallback_name
            except Exception as fallback_exc:
                logger.error(
                    "Both PDF backends failed. primary=%s fallback=%s",
                    primary_exc,
                    fallback_exc,
                )
                return DocumentResult(
                    pages=[],
                    total_pages=0,
                    is_scanned=False,
                    backend_used="none",
                )

        # Determine if the majority of pages look scanned
        scanned_count = sum(1 for p in pages if p.char_count < _SCANNED_CHAR_THRESHOLD)
        is_scanned = len(pages) > 0 and scanned_count > len(pages) * 0.5

        logger.debug(
            "Extracted %d pages (%d scanned) via %s",
            len(pages),
            scanned_count,
            backend_used,
        )

        return DocumentResult(
            pages=pages,
            total_pages=len(pages),
            is_scanned=is_scanned,
            backend_used=backend_used,
        )

    def capabilities(self) -> dict[str, Any]:
        return {
            "ocr_capable": False,  # Pure text extraction; vision tier handles OCR
            "max_file_bytes": _MAX_BYTES,
            "backend": self._backend.value,
            "fallback_backend": "pypdf" if self._backend == _PDFBackend.PYMUPDF else "pymupdf",
            "scanned_detection": True,
        }
