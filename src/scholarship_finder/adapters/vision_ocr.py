"""Vision OCR adapter using Gemini Vision API.

Converts PDF pages to images and uses Gemini Vision to extract text.
Only used when PyMuPDF extracts < 50 characters (likely scanned PDF).

Privacy note: Images are sent to Gemini Vision API for OCR processing.
PII scrubbing happens AFTER OCR extraction, not before (cannot scrub images).
"""
from __future__ import annotations

import base64
import logging
from typing import Any, Callable

from scholarship_finder.ports.llm import LLMPort, TaskSpec
from scholarship_finder.domain.models import LLMTier

logger = logging.getLogger(__name__)

_OCR_PROMPT = """\
Extract all text from this document image.

Return ONLY the extracted text, with no additional commentary or explanation.
Preserve the structure of the document as much as possible (headings, paragraphs, lists).
"""


class VisionOCRAdapter:
    """OCR adapter using Gemini Vision API.

    Converts PDF pages to images and uses Gemini Vision to extract text.
    """

    def __init__(self, llm_port: LLMPort, vision_model: str, max_pages: int = 20):
        """Initialize the OCR adapter.

        Args:
            llm_port: LLM port with vision capability (e.g., Gemini)
            vision_model: Model ID to use for vision tasks
            max_pages: Maximum number of pages to OCR in a single PDF (cost control)
        """
        self._llm = llm_port
        self._vision_model = vision_model
        self._max_pages = max_pages

    async def ocr_page(self, image_bytes: bytes, page_num: int) -> str:
        """Extract text from a single page image using vision.

        Args:
            image_bytes: Image data (PNG/JPEG format)
            page_num: Page number (for logging)

        Returns:
            Extracted text from the image
        """
        logger.debug("OCR: Processing page %d (%d bytes)", page_num, len(image_bytes))

        # Convert image to base64
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")

        # Build task spec with image
        task = TaskSpec(
            tier=LLMTier.VISION,
            instruction=_OCR_PROMPT,
            data_blocks=[
                {
                    "type": "image",
                    "data": image_b64,
                    "mime_type": "image/png",
                }
            ],
            output_schema=None,
            max_output_tokens=4096,
            temperature=0.1,  # Low temperature for deterministic OCR
        )

        try:
            result = await self._llm.complete(task)
            text = result.content.strip()
            logger.debug("OCR: Page %d extracted %d chars", page_num, len(text))
            return text
        except Exception as exc:
            logger.warning("OCR: Failed for page %d: %s", page_num, exc)
            return ""

    async def ocr_pdf(
        self,
        pdf_bytes: bytes,
        page_indices: list[int],
        render_fn: Callable[[bytes, int], bytes],
    ) -> dict[int, str]:
        """OCR specific pages from PDF.

        Args:
            pdf_bytes: Raw PDF bytes
            page_indices: List of 0-indexed page numbers to OCR
            render_fn: Function that renders a page to image bytes: (pdf_bytes, page_idx) -> bytes

        Returns:
            Mapping of page_num (1-indexed) -> extracted text
        """
        if not page_indices:
            return {}

        # Limit number of pages to OCR
        pages_to_ocr = page_indices[: self._max_pages]
        if len(pages_to_ocr) < len(page_indices):
            logger.warning(
                "OCR: Limiting to %d pages (requested %d) due to max_pages limit",
                len(pages_to_ocr),
                len(page_indices),
            )

        results: dict[int, str] = {}

        # Render pages to images and OCR in parallel
        import asyncio

        async def process_page(page_idx: int) -> tuple[int, str]:
            try:
                image_bytes: bytes = await asyncio.to_thread(render_fn, pdf_bytes, page_idx)
                text = await self.ocr_page(image_bytes, page_idx + 1)
                return page_idx + 1, text  # Return 1-indexed page number
            except Exception as exc:
                logger.warning("OCR: Failed to render/OCR page %d: %s", page_idx + 1, exc)
                return page_idx + 1, ""

        tasks = [process_page(idx) for idx in pages_to_ocr]
        ocr_results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in ocr_results:
            if isinstance(result, Exception):
                logger.warning("OCR: Exception in task: %s", result)
            elif isinstance(result, tuple) and len(result) == 2:
                page_num, text = result
                results[page_num] = text
            else:
                logger.warning("OCR: Unexpected result type: %s", type(result))

        logger.info("OCR: Completed %d pages, extracted %d total chars", len(results), sum(len(t) for t in results.values()))
        return results

    def capabilities(self) -> dict[str, Any]:
        """Return OCR adapter capabilities."""
        return {
            "max_pages": self._max_pages,
            "vision_model": self._vision_model,
            "supports_images": True,
        }
