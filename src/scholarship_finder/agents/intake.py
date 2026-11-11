"""Intake Service — multi-document + form-profile intake with PII scrubbing.

Accepts up to 5 files (pdf/txt/md/docx, <=10 MB each, magic-byte check for
pdf/docx) plus a form CandidateProfile. Every document and every free-text
form field is scrubbed with the same PIIScrubber + per-run PIIVault before
anything leaves this service. Requires >=1 document OR a form with degree +
field + target countries.
"""
from __future__ import annotations

import logging
import re

from scholarship_finder.adapters import document_text
from scholarship_finder.domain.models import (
    CandidateProfile,
    IntakeBundle,
    ScrubbedDocument,
)
from scholarship_finder.domain.pii import PIIScrubber, PIIVault, _GITHUB_URL_RE
from scholarship_finder.ports.document import DocumentPort

logger = logging.getLogger(__name__)

_MAX_FILE_BYTES = 10 * 1024 * 1024   # 10 MB
_MAX_FILES = 5
_ALLOWED_EXT = (".pdf", ".txt", ".md", ".markdown", ".docx")
_MAGIC = {".pdf": b"%PDF", ".docx": b"PK\x03\x04"}

_GPA_RE = re.compile(
    r"(?:gpa|cgpa|grade)[:\s]*([0-9]+(?:\.[0-9]+)?)\s*(?:/\s*([0-9]+(?:\.[0-9]+)?))?",
    re.IGNORECASE,
)


class IntakeService:
    """Validates, extracts and PII-scrubs uploaded documents + form data."""

    def __init__(self, document_port: DocumentPort, vault_ttl_seconds: int = 900) -> None:
        self._doc_port = document_port
        self._vault_ttl = vault_ttl_seconds

    async def process_documents(
        self,
        files: list[tuple[bytes, str, str]],
        form_profile: CandidateProfile | None = None,
        display_name: str | None = None,
        github_username: str | None = None,
    ) -> tuple[IntakeBundle, PIIVault]:
        """Build an IntakeBundle from uploaded files + optional form profile.

        ``files``: list of (bytes, filename, label) — label in
        RESUME|TRANSCRIPT|OTHER. Raises ValueError on validation failure.
        """
        if len(files) > _MAX_FILES:
            raise ValueError(f"At most {_MAX_FILES} files allowed (got {len(files)})")
        vault = PIIVault(ttl_seconds=self._vault_ttl)
        scrubber = PIIScrubber(display_name=display_name)
        warnings: list[str] = []
        documents: list[ScrubbedDocument] = []

        for file_bytes, filename, label in files:
            doc, warns = await self._process_file(file_bytes, filename, label, scrubber, vault)
            documents.append(doc)
            warnings.extend(warns)

        # Scrub free-text form fields (scrubber handles name/email/phone/ids)
        if form_profile is not None:
            form_profile = self._scrub_form(form_profile, scrubber, vault)

        if not documents and not _form_sufficient(form_profile):
            raise ValueError(
                "Provide at least one document, or a form with degree, field "
                "and target countries filled in"
            )

        gh = github_username or _github_from_docs(documents)
        bundle = IntakeBundle(
            documents=documents,
            form_profile=form_profile,
            github_username=gh,
            has_display_name=bool(display_name),
            warnings=warnings,
        )
        return bundle, vault

    # ── internals ────────────────────────────────────────────────────────────

    async def _process_file(
        self, file_bytes: bytes, filename: str, label: str,
        scrubber: PIIScrubber, vault: PIIVault,
    ) -> tuple[ScrubbedDocument, list[str]]:
        name = (filename or "").lower()
        ext = "." + name.rsplit(".", 1)[-1] if "." in name else ""
        if ext not in _ALLOWED_EXT:
            raise ValueError(f"Unsupported file type: {filename}")
        if len(file_bytes) > _MAX_FILE_BYTES:
            raise ValueError(f"File too large: {filename} exceeds 10 MB")
        magic = _MAGIC.get(ext)
        warnings: list[str] = []
        if magic and not file_bytes[: len(magic)] == magic:
            warnings.append(f"{filename}: content does not look like {ext} — trying anyway")

        pages = 1
        if ext == ".pdf":
            res = await self._doc_port.extract(file_bytes, max_pages=50)
            text = res.full_text()
            pages = res.total_pages or len(res.pages) or 1
            if res.is_scanned:
                warnings.append(f"{filename}: looks scanned — OCR used")
        elif document_text.is_text_like(filename):
            text = await _to_thread(document_text.extract_text, file_bytes, filename)
        else:
            raise ValueError(f"Unsupported file type: {filename}")
        if not (text or "").strip():
            raise ValueError(f"Could not extract any text from {filename}")
        scrubbed = scrubber.scrub(text, vault)
        label_u = label.upper() if label else "OTHER"
        if label_u not in ("RESUME", "TRANSCRIPT", "OTHER"):
            label_u = "OTHER"
        return ScrubbedDocument(
            label=label_u, filename=filename, text=scrubbed,
            pages=pages, warnings=warnings,
        ), warnings

    def _scrub_form(
        self, profile: CandidateProfile, scrubber: PIIScrubber, vault: PIIVault
    ) -> CandidateProfile:
        data = profile.model_dump()
        for section in ("financial_need",):
            ctx = data.get(section, {}).get("context")
            if ctx:
                data[section]["context"] = scrubber.scrub(ctx, vault)
        return CandidateProfile.model_validate(data)


def _form_sufficient(profile: CandidateProfile | None) -> bool:
    if profile is None:
        return False
    has_degree = bool(profile.preferences.degree_level) or bool(profile.education)
    has_field = bool(profile.study_fields())
    has_countries = bool(profile.preferences.countries)
    return has_degree and has_field and has_countries


def _github_from_docs(documents: list[ScrubbedDocument]) -> str | None:
    gh_re = re.compile(r"github\.com/([A-Za-z0-9][A-Za-z0-9_-]*)", re.I)
    for doc in documents:
        m = _GITHUB_URL_RE.search(doc.text) or gh_re.search(doc.text)
        if m:
            handle = m.group(1) if m.lastindex else gh_re.search(doc.text).group(1)
            if handle:
                return handle.rstrip("/")
    return None


async def _to_thread(fn, *args):
    import asyncio
    return await asyncio.to_thread(fn, *args)
