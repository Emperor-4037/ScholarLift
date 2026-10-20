"""Intake Service — pre-agent preprocessing step.

Responsibilities:
1. Validate upload (file size, MIME type)
2. Extract PDF/text in-memory via DocumentPort
3. Deterministically scrub PII → PIIVault (before any text leaves this service)
4. Return scrubbed material + vault_id for downstream agents

This is NOT an LLM call — purely deterministic. It runs before the LangGraph
engine fires, called directly from RunGateway.start_run() via the UI layer.

Architecture reference: §1.2 "Intake Service"
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from scholarship_finder.domain.pii import PIIScrubber, PIIVault
from scholarship_finder.ports.document import DocumentPort, DocumentResult

logger = logging.getLogger(__name__)

# Limits
_MAX_FILE_BYTES = 10 * 1024 * 1024   # 10 MB
_ALLOWED_MIME_TYPES = frozenset({
    "application/pdf",
    "text/plain",
    "text/markdown",
})


@dataclass
class IntakeResult:
    """Output of the Intake Service."""

    # Scrubbed text extracted from the uploaded document
    scrubbed_text: str
    # Original filename (not scrubbed — it may contain PII, keep internal)
    original_filename: str
    # ID of the PIIVault holding the real values
    vault_id: str
    # PDF extraction metadata
    total_pages: int
    is_scanned: bool
    backend_used: str
    # Warnings (e.g. scanned PDF detected, some pages empty)
    warnings: list[str]


class IntakeService:
    """Validates and pre-processes uploaded candidate documents.

    Raises
    ------
    ValueError
        On invalid file type, oversized file, or completely empty extraction.
    """

    def __init__(
        self,
        document_port: DocumentPort,
        vault_ttl_seconds: int = 900,
    ) -> None:
        self._doc_port = document_port
        self._vault_ttl = vault_ttl_seconds

    async def process(
        self,
        file_bytes: bytes,
        filename: str,
        mime_type: str,
        display_name: str | None = None,
    ) -> tuple[IntakeResult, PIIVault]:
        """Process an uploaded file and return scrubbed text + PII vault.

        Parameters
        ----------
        file_bytes:
            Raw bytes of the uploaded file.
        filename:
            Original filename (for logging / metadata only).
        mime_type:
            Detected MIME type — validated against allowlist.
        display_name:
            Optional candidate name to scrub out of the text.

        Returns
        -------
        (IntakeResult, PIIVault)
            The vault must be passed to downstream agents for rehydration.
        """
        warnings: list[str] = []

        # ── Validation ────────────────────────────────────────────────────────
        self._validate(file_bytes, mime_type, filename)

        # ── Extraction ────────────────────────────────────────────────────────
        if mime_type == "application/pdf":
            doc_result = await self._doc_port.extract(file_bytes, max_pages=50)
            raw_text = doc_result.full_text()
            total_pages = doc_result.total_pages
            is_scanned = doc_result.is_scanned
            backend_used = doc_result.backend_used

            if is_scanned:
                warnings.append(
                    f"Document appears to be scanned ({total_pages} pages with minimal text). "
                    "Extraction quality may be reduced. Consider uploading a text-based PDF."
                )
        else:
            # Plain text / Markdown
            raw_text = file_bytes.decode("utf-8", errors="replace")
            total_pages = 1
            is_scanned = False
            backend_used = "plaintext"

        if not raw_text.strip():
            raise ValueError(
                "Could not extract any text from the uploaded document. "
                "Please upload a text-based PDF or plain-text resume."
            )

        # Warn on very short extractions
        if len(raw_text.strip()) < 200:
            warnings.append(
                f"Very little text was extracted ({len(raw_text.strip())} chars). "
                "Results may be limited."
            )

        # ── PII Scrubbing ─────────────────────────────────────────────────────
        vault = PIIVault(ttl_seconds=self._vault_ttl)
        scrubber = PIIScrubber(display_name=display_name)
        scrubbed_text = scrubber.scrub(raw_text, vault)

        logger.info(
            "Intake: file=%r pages=%d scanned=%s chars_raw=%d chars_scrubbed=%d vault_id=%s",
            filename,
            total_pages,
            is_scanned,
            len(raw_text),
            len(scrubbed_text),
            vault.vault_id,
        )

        result = IntakeResult(
            scrubbed_text=scrubbed_text,
            original_filename=filename,
            vault_id=vault.vault_id,
            total_pages=total_pages,
            is_scanned=is_scanned,
            backend_used=backend_used,
            warnings=warnings,
        )
        return result, vault

    # ── Internal ──────────────────────────────────────────────────────────────

    def _validate(self, file_bytes: bytes, mime_type: str, filename: str) -> None:
        if not file_bytes:
            raise ValueError("Uploaded file is empty.")
        if len(file_bytes) > _MAX_FILE_BYTES:
            mb = len(file_bytes) / 1024 / 1024
            raise ValueError(
                f"File too large ({mb:.1f} MB). Maximum allowed size is "
                f"{_MAX_FILE_BYTES // 1024 // 1024} MB."
            )
        if mime_type not in _ALLOWED_MIME_TYPES:
            raise ValueError(
                f"Unsupported file type: {mime_type!r}. "
                f"Accepted types: {sorted(_ALLOWED_MIME_TYPES)}"
            )
