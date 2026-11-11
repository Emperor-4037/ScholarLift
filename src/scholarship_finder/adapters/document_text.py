"""Plain-text and DOCX document extraction helpers.

Complements ``document_pdf.py`` for the multi-document intake:
``.txt``/``.md`` are decoded directly; ``.docx`` goes through python-docx
(paragraphs + tables). Everything is pure/sync — callers run it in a thread
when inside an event loop.
"""
from __future__ import annotations

import logging

from scholarship_finder.errors import TransientError

logger = logging.getLogger(__name__)

_TEXT_EXTENSIONS = (".txt", ".md", ".markdown")
_DOCX_EXTENSIONS = (".docx",)


def is_text_like(filename: str) -> bool:
    """True for extensions this module can extract."""
    name = filename.lower()
    return name.endswith(_TEXT_EXTENSIONS + _DOCX_EXTENSIONS)


def extract_text(file_bytes: bytes, filename: str) -> str:
    """Extract text from .txt/.md/.docx bytes.

    Raises:
        TransientError: when .docx parsing fails.
        ValueError:     for unsupported extensions.
    """
    name = filename.lower()
    if name.endswith(_TEXT_EXTENSIONS):
        return _decode(file_bytes)
    if name.endswith(_DOCX_EXTENSIONS):
        return _extract_docx(file_bytes)
    raise ValueError(f"Unsupported text document type: {filename}")


def _decode(file_bytes: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return file_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    return file_bytes.decode("utf-8", errors="replace")


def _extract_docx(file_bytes: bytes) -> str:
    import io

    try:
        import docx
    except ImportError as exc:  # pragma: no cover
        raise TransientError(
            "document_text", "python-docx not installed"
        ) from exc

    try:
        document = docx.Document(io.BytesIO(file_bytes))
    except Exception as exc:
        raise TransientError("document_text", f"DOCX parse failed: {exc}") from exc

    parts: list[str] = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)
