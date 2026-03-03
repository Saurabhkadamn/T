"""
Central configuration for Kadal AI Deep Research Microservice.

This is the ONLY file where default values and model names are hardcoded.
All node files must import from here — no magic numbers or literal model
name strings elsewhere in the codebase.

Env vars are loaded from `.env` (or the environment).
Nested settings use double-underscore delimiter, e.g.:
  LOOPS__CLARIFICATION_MAX=5
  DEPTH_IN_DEPTH__MAX_SOURCES_PER_SECTION=15
  MODELS__PRO_MODEL=gemini-2.5-pro-latest
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Sub-models (no env loading — composed into Settings)
# ---------------------------------------------------------------------------

class DepthConfig(BaseModel):
    """Per-depth-level operational parameters.

    compression_target_tokens is depth-dependent because the number of
    unique, non-overlapping facts scales with source count.  Forcing all
    depths to the same 3-5K window silently drops valid citations at higher
    depths.  See arch.md §7 for rationale.
    """

    max_search_iterations: int
    max_sources_per_section: int
    compression_target_tokens: int
    supervisor_reflection_enabled: bool


class LoopLimits(BaseModel):
    """Hard caps on every feedback / revision loop in both graphs.

    These are enforced inside graph routing functions — not in node logic.
    """

    clarification_max: int = 3
    plan_refinement_max: int = 3
    plan_revision_max: int = 3          # max user plan rejections before forcing restart
    plan_self_review_max: int = 2
    supervisor_reflection_max: int = 2
    report_revision_max: int = 1


class ModelConfig(BaseModel):
    """LLM model identifiers and per-task tier assignments.

    use_pro_for_* flags let you override a single task to Flash (e.g. in
    tests or cost experiments) without touching node code.

    Convention: True = Pro tier, False = Flash tier.
    """

    pro_model: str = "gemini-2.5-pro"
    flash_model: str = "gemini-2.5-flash"

    # OpenAI model identifiers
    pro_model_openai: str = "gpt-4o"
    flash_model_openai: str = "gpt-4o-mini"

    # Anthropic model identifiers
    pro_model_anthropic: str = "claude-opus-4-6"
    flash_model_anthropic: str = "claude-haiku-4-5-20251001"

    # --- Pro tasks (reasoning-heavy) ---
    use_pro_for_query_analysis: bool = True
    use_pro_for_plan_creation: bool = True
    use_pro_for_plan_review: bool = True
    use_pro_for_supervisor_reflection: bool = True
    use_pro_for_knowledge_fusion: bool = True
    use_pro_for_report_writing: bool = True
    use_pro_for_report_review: bool = True

    # --- Flash tasks (mechanical / high-volume) ---
    # Only override to True for A/B testing; production keeps these False.
    use_pro_for_context_building: bool = False  # Flash by default (spec §Settings)
    use_pro_for_query_gen: bool = False
    use_pro_for_compression: bool = False
    use_pro_for_scoring: bool = False
    

    def model_for(self, task: str) -> str:
        """Return the resolved model string for a named task.

        Usage::
            llm = get_llm(settings.models.model_for("query_analysis"))

        Valid task names match the ``use_pro_for_*`` field suffixes:
        ``query_analysis``, ``plan_creation``, ``plan_review``,
        ``supervisor_reflection``, ``knowledge_fusion``, ``report_writing``,
        ``report_review``, ``query_gen``, ``compression``, ``scoring``.
        """
        field_name = f"use_pro_for_{task}"
        use_pro: bool = getattr(self, field_name, False)
        return self.pro_model if use_pro else self.flash_model

    def model_for_provider(self, task: str, provider: str) -> str:
        """Return the model name for a task on a specific provider.

        Used by ``llm_factory.get_llm`` when the active provider is not Google.
        Falls back to Google model names for unknown providers.
        """
        field_name = f"use_pro_for_{task}"
        use_pro: bool = getattr(self, field_name, False)
        if provider == "openai":
            return self.pro_model_openai if use_pro else self.flash_model_openai
        if provider == "anthropic":
            return self.pro_model_anthropic if use_pro else self.flash_model_anthropic
        # "google" or unknown — fall back to google names
        return self.pro_model if use_pro else self.flash_model


class RedisTTLConfig(BaseModel):
    """TTL values (in seconds) for every Redis key schema entry.

    See CLAUDE.md § Redis Key Schema for key definitions.
    """

    job_status_ttl_seconds: int = 86_400        # 24 hr
    source_score_ttl_seconds: int = 604_800     # 7 days
    rate_limit_plan_ttl_seconds: int = 3_600    # 1 hr window
    rate_limit_run_ttl_seconds: int = 3_600     # 1 hr window


# ---------------------------------------------------------------------------
# Root settings (reads from environment / .env)
# ---------------------------------------------------------------------------

DepthLevel = Literal["surface", "intermediate", "in-depth"]


class Settings(BaseSettings):
    """Application-wide settings loaded from environment variables.

    Nested models are overridable via double-underscore env vars::

        DEPTH_SURFACE__MAX_SOURCES_PER_SECTION=4
        LOOPS__PLAN_SELF_REVIEW_MAX=1

    Required vars (no defaults — startup fails if missing):
        MONGO_URI, REDIS_HOST, GEMINI_API_KEY, S3_BUCKET,
        LANGFUSE_HOST, OTEL_COLLECTOR_ENDPOINT,
        TAVILY_API_KEY, SERPER_API_KEY
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # External service endpoints / credentials (required — no defaults)
    # ------------------------------------------------------------------
    mongo_uri: str
    redis_host: str
    redis_password: str = ""
    gemini_api_key: str
    s3_bucket: str
    langfuse_host: str
    otel_collector_endpoint: str
    tavily_api_key: str
    serper_api_key: str
    content_lake_url: str = ""          # optional — only used when tools.content_lake=true

    # ------------------------------------------------------------------
    # LLM provider selection
    # ------------------------------------------------------------------
    llm_provider: Literal["google", "openai", "anthropic"] = "google"
    openai_api_key: str = ""  
    openai_base_url: str = "https://api.openai.com/v1"          # required only when llm_provider=openai
    anthropic_api_key: str = ""         # required only when llm_provider=anthropic

    # ------------------------------------------------------------------
    # Depth-level presets
    # ------------------------------------------------------------------
    depth_surface: DepthConfig = DepthConfig(
        max_search_iterations=1,
        max_sources_per_section=3,
        compression_target_tokens=2_500,
        supervisor_reflection_enabled=False,
    )
    depth_intermediate: DepthConfig = DepthConfig(
        max_search_iterations=2,
        max_sources_per_section=5,
        compression_target_tokens=5_000,
        supervisor_reflection_enabled=False,
    )
    depth_in_depth: DepthConfig = DepthConfig(
        max_search_iterations=3,
        max_sources_per_section=10,
        compression_target_tokens=8_000,
        supervisor_reflection_enabled=True,
    )

    # ------------------------------------------------------------------
    # Loop limits
    # ------------------------------------------------------------------
    loops: LoopLimits = LoopLimits()

    # ------------------------------------------------------------------
    # Model assignments
    # ------------------------------------------------------------------
    models: ModelConfig = ModelConfig()

    # ------------------------------------------------------------------
    # Redis TTLs
    # ------------------------------------------------------------------
    redis_ttls: RedisTTLConfig = RedisTTLConfig()

    # ------------------------------------------------------------------
    # Resilience / partial-failure
    # ------------------------------------------------------------------
    # If the fraction of failed sections exceeds this, the job is marked
    # FAILED rather than PARTIAL_SUCCESS.
    partial_failure_threshold: float = 0.5

    # ------------------------------------------------------------------
    # CORS
    # ------------------------------------------------------------------
    cors_origins: list[str] = ["*"]   # override via CORS_ORIGINS='["https://app.example.com"]'

    # ------------------------------------------------------------------
    # MongoDB database name
    # ------------------------------------------------------------------
    mongo_db_name: str = "Dev_Kadal"    # override: MONGO_DB_NAME=Prod_Kadal

    # ------------------------------------------------------------------
    # S3 settings
    # ------------------------------------------------------------------
    s3_region: str = "ap-south-1"                      # override: S3_REGION=us-east-1
    s3_presigned_url_ttl_seconds: int = 3600            # 1 hour

    # ------------------------------------------------------------------
    # LLM call timeout
    # ------------------------------------------------------------------
    llm_timeout_seconds: int = 120                      # override: LLM_TIMEOUT_SECONDS=180

    # ------------------------------------------------------------------
    # Connection pool sizes
    # ------------------------------------------------------------------
    mongo_max_pool_size: int = 50
    redis_max_connections: int = 100

    # ------------------------------------------------------------------
    # Planning graph timeout
    # ------------------------------------------------------------------
    planning_timeout_seconds: int = 180

    # ------------------------------------------------------------------
    # Rate limits (None = unlimited — revisit once tenancy model is set)
    # ------------------------------------------------------------------
    rate_limit_plan_per_hour: int | None = None
    rate_limit_run_per_hour: int | None = None
    max_concurrent_jobs_per_tenant: int | None = None

    # ------------------------------------------------------------------
    # Startup validation
    # ------------------------------------------------------------------
    @model_validator(mode="after")
    def validate_llm_keys(self) -> "Settings":
        """Fail fast at startup if the active LLM provider has no API key."""
        if self.llm_provider == "openai" and not self.openai_api_key:
            raise ValueError("OPENAI_API_KEY must be set when LLM_PROVIDER=openai")
        if self.llm_provider == "anthropic" and not self.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY must be set when LLM_PROVIDER=anthropic")
        if self.llm_provider == "google" and not self.gemini_api_key:
            raise ValueError("GEMINI_API_KEY must be set when LLM_PROVIDER=google")
        return self

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def get_depth_config(self, depth: DepthLevel) -> DepthConfig:
        """Return the DepthConfig for a given depth level string.

        Falls back to ``depth_intermediate`` for unrecognised values so
        nodes never crash on a bad input.

        Usage::
            cfg = settings.get_depth_config(state["depth_of_research"])
            max_sources = cfg.max_sources_per_section
        """
        mapping: dict[str, DepthConfig] = {
            "surface": self.depth_surface,
            "intermediate": self.depth_intermediate,
            "in-depth": self.depth_in_depth,
        }
        return mapping.get(depth, self.depth_intermediate)

    def get_compression_target(self, depth: DepthLevel) -> int:
        """Convenience wrapper used by compressor.py.

        Usage::
            target_tokens = settings.get_compression_target(state["depth_of_research"])
        """
        return self.get_depth_config(depth).compression_target_tokens


# ---------------------------------------------------------------------------
# Module-level singleton — import this everywhere
# ---------------------------------------------------------------------------
# Node files should do:
#     from app.config import settings
# Never instantiate Settings() directly in node files.

settings = Settings()
