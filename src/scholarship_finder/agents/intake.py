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

from scholarship_finder.domain.pii import PIIScrubber, PIIVault, _GITHUB_URL_RE
from scholarship_finder.ports.document import DocumentPort

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
    # OCR metadata
    ocr_used: bool = False
    ocr_backend: str = ""
    # Extracted GitHub username from resume URLs
    github_username: str | None = None
    # Extracted candidate name from resume (if not provided)
    extracted_name: str | None = None


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
        ocr_used = False
        ocr_backend = ""
        
        if mime_type == "application/pdf":
            doc_result = await self._doc_port.extract(file_bytes, max_pages=50)
            raw_text = doc_result.full_text()
            total_pages = doc_result.total_pages
            is_scanned = doc_result.is_scanned
            backend_used = doc_result.backend_used
            ocr_used = doc_result.ocr_used
            ocr_backend = doc_result.ocr_backend

            if is_scanned:
                if doc_result.ocr_used:
                    warnings.append(
                        f"Document was scanned ({total_pages} pages). "
                        f"OCR was applied using {doc_result.ocr_backend} to extract text. "
                        "Quality may vary - please review extracted text."
                    )
                else:
                    warnings.append(
                        f"Document appears to be scanned ({total_pages} pages with minimal text). "
                        "OCR is disabled or unavailable. Extraction quality may be reduced. "
                        "Consider uploading a text-based PDF."
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

        # ── Name Extraction (if not provided) ─────────────────────────────────
        extracted_name = None
        if not display_name or not display_name.strip():
            extracted_name = self._extract_name(raw_text)
            display_name = extracted_name
            if display_name:
                logger.info("Intake: extracted name from resume: %s", display_name)

        # ── PII Scrubbing ─────────────────────────────────────────────────────
        vault = PIIVault(ttl_seconds=self._vault_ttl)
        scrubber = PIIScrubber(display_name=display_name)
        scrubbed_text = scrubber.scrub(raw_text, vault)

        # ── GitHub Username Extraction ───────────────────────────────────────
        github_username = self._extract_github_username(raw_text)

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
            ocr_used=ocr_used,
            ocr_backend=ocr_backend,
            warnings=warnings,
            github_username=github_username,
            extracted_name=extracted_name,
        )
        return result, vault

    # ── Internal ──────────────────────────────────────────────────────────────

    def _extract_name(self, text: str) -> str | None:
        """Extract candidate name from resume text.

        Looks for name patterns at the beginning of the document.
        Returns None if no clear name is found.
        """
        import re

        # Get first few lines (where name typically appears)
        lines = text.split('\n')[:10]
        first_text = '\n'.join(lines).strip()

        # Pattern 1: Name at the very beginning (no digits, no URLs, proper case)
        # Match lines that look like names (2-4 words, capitalized, no special chars)
        name_pattern = re.compile(
            r'^([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})$',
            re.MULTILINE
        )

        # Try to find a name in the first few lines
        for line in lines[:5]:
            line = line.strip()
            if not line:
                continue

            # Skip lines with obvious non-name content
            if any(x in line.lower() for x in ['resume', 'cv', 'curriculum', 'vitae', 'experience', 'education', 'objective', 'summary', 'email', 'phone', 'linkedin', 'github', 'portfolio', 'http', 'www', '.com', '@']):
                continue

            # Skip lines with digits
            if re.search(r'\d', line):
                continue

            # Try to match name pattern
            match = name_pattern.match(line)
            if match:
                candidate_name = match.group(1).strip()
                # Basic validation: should be 2-4 words, all start with capital
                words = candidate_name.split()
                if 2 <= len(words) <= 4 and all(w[0].isupper() for w in words):
                    logger.info("Intake: extracted name '%s' from line: %s", candidate_name, line)
                    return candidate_name

        logger.debug("Intake: could not extract name from resume")
        return None

    def _extract_github_username(self, text: str) -> str | None:
        """Extract GitHub username from URLs in text."""
        matches = _GITHUB_URL_RE.findall(text)
        if matches:
            logger.debug("Intake: found %d GitHub URL(s) in text", len(matches))
            # Extract username from URL (last segment)
            for url in matches:
                parts = url.rstrip('/').split('/')
                if len(parts) >= 2:
                    username = parts[-1]
                    if username and not username in ['github.com', 'www.github.com']:
                        logger.info("Intake: extracted GitHub username: %s from URL: %s", username, url)
                        return username
        else:
            logger.debug("Intake: no GitHub URLs found in text")
        return None

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
