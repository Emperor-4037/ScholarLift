"""Typed application configuration loaded from environment variables / `.env`.

Model IDs never appear in agent logic — only here, as ordered per-tier chains.
The composition root reads Settings once at startup and wires adapters from it.
"""
from __future__ import annotations

import json
import warnings
from enum import Enum
from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DeploymentProfile(str, Enum):
    LITE = "LITE"   # no headless browser
    FULL = "FULL"   # enables the Playwright fetch tier


class SearchSecondaryProvider(str, Enum):
    JINA = "jina"
    SERPER = "serper"


class PDFBackend(str, Enum):
    PYMUPDF = "pymupdf"
    PYPDF = "pypdf"


# Default per-model limits (requests/min, tokens/min, requests/day).
# Groq free tier verified via x-ratelimit headers: 1000 RPD, 8000 TPM per model.
_DEFAULT_GROQ_LIMITS = {"rpm": 30, "tpm": 8000, "rpd": 1000}
_DEFAULT_GEMINI_LIMITS = {"rpm": 10, "tpm": 250_000, "rpd": 250}
_DEFAULT_OTHER_LIMITS = {"rpm": 60, "tpm": 200_000, "rpd": 10_000}


def _split_chain(value: str) -> list[str]:
    return [m.strip() for m in value.split(",") if m.strip()]


class Settings(BaseSettings):
    """All application settings, validated at startup. Secrets never leave the server."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    deployment_profile: DeploymentProfile = DeploymentProfile.LITE

    # ── LLM providers ──────────────────────────────────────────────────────
    gemini_api_key: str = ""
    groq_api_key: str = ""
    groq_api_base_url: str = "https://api.groq.com/openai/v1"
    openai_api_key: str = ""
    openai_api_base_url: str = ""

    # Ordered, comma-separated model chains (first = preferred). Routing rule:
    # "gemini-*" → Gemini; anything else → Groq (or OpenAI-compatible endpoint).
    # An explicit "groq:", "gemini:" or "openai:" prefix overrides the rule.
    tier_fast_chain: str = (
        "local:qwen2.5-7b-instruct,openai/gpt-oss-20b,qwen/qwen3.8-27b,"
        "gemini-3.6-flash,gemini-3.5-flash-lite,gemini-3.8-flash"
    )
    tier_reasoning_chain: str = (
        "openai/gpt-oss-120b,local:qwen2.5-7b-instruct,gemini-3.6-flash,"
        "qwen/qwen3.8-27b,gemini-3.8-flash"
    )
    tier_vision_chain: str = "gemini-3.6-flash,gemini-3.5-flash,gemini-3.8-flash"

    # ── Local LLM (llama.cpp) ──────────────────────────────────────────────
    local_llm_enabled: bool = True
    local_llm_base_url: str = "http://127.0.0.1:8088/v1"
    local_llm_model: str = "qwen2.5-7b-instruct"
    local_llm_parallel: Annotated[int, Field(ge=1, le=8)] = 3
    local_llm_ctx: Annotated[int, Field(ge=2048, le=131072)] = 18432
    local_llm_max_input_tokens: Annotated[int, Field(ge=256, le=100000)] = 4500
    local_llm_autostart: bool = True
    local_llm_server_path: str = ""
    local_llm_model_path: str = ""
    local_llm_extra_args: str = "-fa on"
    # Optional JSON override: {"openai/gpt-oss-20b": {"rpm": 30, "tpm": 8000, "rpd": 1000}}
    model_limits_json: str = ""

    max_reasoning_calls_per_run: Annotated[int, Field(ge=1, le=50)] = 6
    max_llm_calls_per_run: Annotated[int, Field(ge=5, le=500)] = 90

    # ── Search ─────────────────────────────────────────────────────────────
    tavily_api_key: str = ""
    search_secondary_provider: SearchSecondaryProvider = SearchSecondaryProvider.JINA
    serper_api_key: str = ""
    jina_api_key: str = ""
    max_search_credits_per_run: Annotated[int, Field(ge=5, le=500)] = 60

    # ── Fetch ──────────────────────────────────────────────────────────────
    jina_reader_key: str = ""   # falls back to JINA_API_KEY

    # ── GitHub ─────────────────────────────────────────────────────────────
    github_token: str = ""

    # ── Access / admission ────────────────────────────────────────────────
    demo_access_code: str = ""  # when set, POST /api/runs requires it
    max_concurrent_runs: Annotated[int, Field(ge=1, le=10)] = 3

    # ── Privacy ────────────────────────────────────────────────────────────
    pii_vault_ttl_seconds: Annotated[int, Field(ge=60, le=7200)] = 1800

    # ── Run budget ─────────────────────────────────────────────────────────
    run_soft_deadline_s: Annotated[float, Field(ge=30.0, le=1200.0)] = 240.0
    run_hard_deadline_s: Annotated[float, Field(ge=60.0, le=1800.0)] = 360.0
    run_reserve_after_audit_s: Annotated[float, Field(ge=15.0, le=300.0)] = 90.0
    target_verified_opportunities: Annotated[int, Field(ge=1, le=50)] = 10
    max_audit_candidates: Annotated[int, Field(ge=5, le=200)] = 30

    # ── Knowledge base ────────────────────────────────────────────────────
    kb_enabled: bool = True
    kb_path: str = "runs/knowledge.db"
    kb_opp_ttl_days: int = 7
    kb_search_ttl_hours: int = 24

    # ── OCR ────────────────────────────────────────────────────────────────
    enable_ocr: bool = True
    ocr_threshold_chars: Annotated[int, Field(ge=10, le=500)] = 50
    ocr_max_pages: Annotated[int, Field(ge=1, le=50)] = 10

    # ── API server ─────────────────────────────────────────────────────────
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    api_reload: bool = False

    pdf_backend: PDFBackend = PDFBackend.PYMUPDF

    # ── Validators ─────────────────────────────────────────────────────────

    @field_validator("tier_fast_chain", "tier_reasoning_chain", "tier_vision_chain")
    @classmethod
    def _chain_non_empty(cls, v: str) -> str:
        if not _split_chain(v):
            raise ValueError("Tier chain must contain at least one model ID")
        return v

    @model_validator(mode="before")
    @classmethod
    def _clean_env(cls, data):
        """dotenv reads `KEY=  # comment` as '# comment' — drop it; strip
        surrounding whitespace/quotes from string values."""
        if not isinstance(data, dict):
            return data
        out = {}
        for k, v in data.items():
            if isinstance(v, str):
                v = v.strip()
                if v.startswith("#"):
                    continue
                if len(v) >= 2 and v[0] in "'\"" and v[-1] == v[0]:
                    v = v[1:-1]
            out[k] = v
        return out

    @model_validator(mode="after")
    def _warn_missing(self) -> "Settings":
        if not (self.groq_api_key or self.gemini_api_key or self.openai_api_key):
            warnings.warn("No LLM API key configured (GROQ/GEMINI/OPENAI).", stacklevel=2)
        if not self.tavily_api_key:
            warnings.warn("TAVILY_API_KEY missing — falling back to secondary search.",
                          stacklevel=2)
        if self.run_hard_deadline_s <= self.run_soft_deadline_s:
            self.run_hard_deadline_s = self.run_soft_deadline_s + 60.0
        return self

    # ── Helpers ────────────────────────────────────────────────────────────

    def fast_models(self) -> list[str]:
        return _split_chain(self.tier_fast_chain)

    def reasoning_models(self) -> list[str]:
        return _split_chain(self.tier_reasoning_chain)

    def vision_models(self) -> list[str]:
        return _split_chain(self.tier_vision_chain)

    def effective_jina_reader_key(self) -> str:
        return self.jina_reader_key or self.jina_api_key

    def is_full_profile(self) -> bool:
        return self.deployment_profile == DeploymentProfile.FULL

    def model_limits(self, provider: str, model: str) -> dict[str, int]:
        """Return {"rpm","tpm","rpd"} for a model (override JSON → provider defaults)."""
        base = {
            "groq": _DEFAULT_GROQ_LIMITS,
            "gemini": _DEFAULT_GEMINI_LIMITS,
        }.get(provider, _DEFAULT_OTHER_LIMITS)
        limits = dict(base)
        if self.model_limits_json:
            try:
                override = json.loads(self.model_limits_json).get(model) or {}
                limits.update({k: int(v) for k, v in override.items() if k in limits})
            except (ValueError, AttributeError, TypeError):
                warnings.warn("MODEL_LIMITS_JSON is not valid JSON; ignored.", stacklevel=2)
        return limits


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton Settings instance."""
    return Settings()
