"""Typed application configuration loaded from environment variables.

Model IDs never appear in core logic — only here in config, in ordered chains.
The composition root reads Settings once at startup and wires adapters from it.
"""
from __future__ import annotations

from enum import Enum
from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DeploymentProfile(str, Enum):
    """Runtime deployment profile that gates adapter availability."""
    LITE = "LITE"   # In-process, no headless browser; Streamlit Community Cloud / Render
    FULL = "FULL"   # Container w/ ≥2 GB RAM; enables Tier-3 browser + optional FastAPI/SSE


class SearchSecondaryProvider(str, Enum):
    JINA = "jina"
    SERPER = "serper"


class PDFBackend(str, Enum):
    PYMUPDF = "pymupdf"
    PYPDF = "pypdf"


class Settings(BaseSettings):
    """All application settings, validated at startup.

    Secrets stay server-side; never appear in events or logs.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Deployment ─────────────────────────────────────────────────────────
    deployment_profile: DeploymentProfile = DeploymentProfile.LITE

    # ── LLM ────────────────────────────────────────────────────────────────
    gemini_api_key: str = ""
    groq_api_key: str = ""
    groq_api_base_url: str = "https://api.groq.com/openai/v1"
    openai_api_base_url: str = ""
    openai_api_key: str = ""

    # Ordered comma-separated model IDs; first = highest preference
    # Using only Groq GPT-OSS (Gemini quota exhausted until ~15h from now)
    tier_fast_chain: str = "openai/gpt-oss-20b,openai/gpt-oss-120b"
    tier_reasoning_chain: str = "openai/gpt-oss-120b,openai/gpt-oss-20b"
    tier_vision_chain: str = "openai/gpt-oss-120b"

    # Hard cap from DEC-1: ≤4 reasoning calls per run
    max_reasoning_calls_per_run: int = 4

    # ── Search ─────────────────────────────────────────────────────────────
    tavily_api_key: str = ""
    search_secondary_provider: SearchSecondaryProvider = SearchSecondaryProvider.JINA
    serper_api_key: str = ""
    jina_api_key: str = ""

    # ── Fetch ──────────────────────────────────────────────────────────────
    jina_reader_key: str = ""   # empty → 20 RPM anonymous; keyed → 500 RPM

    # ── GitHub ─────────────────────────────────────────────────────────────
    github_token: str = ""      # public-read scope only

    # ── Demo Safety ────────────────────────────────────────────────────────
    demo_access_code: str = ""  # empty = open (dev mode only)
    judge_reserve_ratio: Annotated[float, Field(ge=0.0, le=1.0)] = 0.30
    max_concurrent_runs: Annotated[int, Field(ge=1, le=10)] = 3

    # ── Privacy ────────────────────────────────────────────────────────────
    pii_vault_ttl_seconds: Annotated[int, Field(ge=60, le=3600)] = 900

    # ── Phase Timing (speed vs quality tradeoff) ───────────────────────────
    # Lower values = faster execution, potentially fewer opportunities
    # Higher values = more thorough search, slower execution
    phase_a_soft_deadline_s: Annotated[float, Field(ge=30.0, le=600.0)] = 90.0
    phase_a_hard_deadline_s: Annotated[float, Field(ge=30.0, le=600.0)] = 120.0
    phase_b_deadline_s: Annotated[float, Field(ge=30.0, le=600.0)] = 90.0

    # ── OCR Configuration ───────────────────────────────────────────────────
    enable_ocr: bool = True  # Enable OCR fallback for scanned PDFs
    ocr_threshold_chars: Annotated[int, Field(ge=10, le=200)] = 50  # Chars below which OCR triggers
    ocr_max_pages: Annotated[int, Field(ge=1, le=50)] = 20  # Max pages to OCR per PDF (cost control)

    # ── API Configuration ─────────────────────────────────────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_reload: bool = True  # Auto-reload in development

    # ── Document ───────────────────────────────────────────────────────────
    pdf_backend: PDFBackend = PDFBackend.PYMUPDF

    # ── Derived / Computed ─────────────────────────────────────────────────

    @field_validator("tier_fast_chain", "tier_reasoning_chain", "tier_vision_chain")
    @classmethod
    def validate_chain_non_empty(cls, v: str) -> str:
        models = [m.strip() for m in v.split(",") if m.strip()]
        if not models:
            raise ValueError("Tier chain must contain at least one model ID")
        return v

    @model_validator(mode="after")
    def warn_missing_keys(self) -> "Settings":
        """Emit warnings (not errors) for missing optional keys at startup."""
        missing = []
        if not self.gemini_api_key:
            missing.append("GEMINI_API_KEY")
        if not self.tavily_api_key:
            missing.append("TAVILY_API_KEY")
        if missing:
            import warnings
            warnings.warn(
                f"Missing optional API keys: {', '.join(missing)}. "
                "Some adapters will be unavailable.",
                stacklevel=2,
            )
        return self

    # ── Helpers ────────────────────────────────────────────────────────────

    def fast_models(self) -> list[str]:
        """Ordered list of fast-tier model IDs."""
        return [m.strip() for m in self.tier_fast_chain.split(",") if m.strip()]

    def reasoning_models(self) -> list[str]:
        """Ordered list of reasoning-tier model IDs."""
        return [m.strip() for m in self.tier_reasoning_chain.split(",") if m.strip()]

    def vision_models(self) -> list[str]:
        """Ordered list of vision-tier model IDs."""
        return [m.strip() for m in self.tier_vision_chain.split(",") if m.strip()]

    def is_full_profile(self) -> bool:
        return self.deployment_profile == DeploymentProfile.FULL

    def demo_mode_enabled(self) -> bool:
        """True if a demo access code is configured (restricts public runs)."""
        return bool(self.demo_access_code)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton Settings instance (cached after first load)."""
    return Settings()
