# -*- coding: utf-8 -*-
"""
@author:XuMing(xuming624@qq.com)
@description: Runtime configuration via pydantic-settings; resolves Settings from init
args, MEMHUB_* env vars and a YAML file, and derives mode-based flags.
"""
from __future__ import annotations

import logging
import os
from importlib import resources
from pathlib import Path
from typing import Any, Literal

from pydantic import field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

logger = logging.getLogger("memhub.config")

DEFAULT_CONFIG_PATH = Path.home() / ".memhub" / "config.yaml"


def config_path() -> Path:
    """Resolve the YAML config path, honoring the MEMHUB_CONFIG_FILE override."""
    override = os.environ.get("MEMHUB_CONFIG_FILE")
    return Path(override).expanduser() if override else DEFAULT_CONFIG_PATH


def _default_config_template() -> str:
    """Load the bundled default YAML shipped inside the memhub package."""
    return (
        resources.files("memhub")
        .joinpath("config.default.yaml")
        .read_text(encoding="utf-8")
    )


def ensure_config_file() -> Path:
    """Create ``~/.memhub/config.yaml`` with defaults when missing (MetaGPT-style bootstrap).

    Skipped when ``MEMHUB_CONFIG_FILE`` points at a custom path — only the default
    home location is auto-created so tests and explicit overrides stay predictable.
    """
    if os.environ.get("MEMHUB_CONFIG_FILE"):
        return config_path()
    path = DEFAULT_CONFIG_PATH
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_default_config_template(), encoding="utf-8")
    logger.info(
        "Created default config at %s — edit jev_api_key, llm_api_key and embed_api_key, "
        "or set MEMHUB_JEV_API_KEY / MEMHUB_LLM_API_KEY / MEMHUB_EMBED_API_KEY.",
        path,
    )
    return path


def ensure_storage_dir(storage_dir: str) -> Path:
    """Create the on-disk data root if missing (SQLite/Chroma/Kuzu need the parent dir)."""
    path = Path(storage_dir).expanduser()
    if not path.is_absolute():
        path = path.resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_app_id(settings: Settings, app_id: str | None) -> str:
    """Return explicit ``app_id`` or ``settings.default_app_id``."""
    return app_id if app_id is not None else settings.default_app_id


def resolve_app_ids(settings: Settings, app_ids: list[str] | None) -> list[str]:
    """Return explicit ``app_ids`` or ``[settings.default_app_id]``."""
    return app_ids if app_ids is not None else [settings.default_app_id]


# Token→char estimate shared by LLM prompt budgeting and embed chunk sizing.
CHARS_PER_TOKEN = 2.5
LLM_CHARS_PER_TOKEN = CHARS_PER_TOKEN
# Embedding input sizing. Deliberately below any real text: dense code and CJK
# run near 1.2 chars/token, and the provider rejects an over-long input instead
# of truncating it, so this budget must err on the safe side.
EMBED_CHARS_PER_TOKEN = 1.2

# LLM context budgeting — override via YAML or MEMHUB_LLM_CONTEXT_WINDOW etc.
LLM_CONTEXT_WINDOW = 32768
LLM_COMPLETION_RESERVE = 4096

# Embed input budget (tokens per API call; longer inputs are head-truncated).
EMBED_MAX_TOKENS = 8000


# Presets change only material budget. Jev remains authoritative in every preset.
PRESETS: dict[str, dict[str, Any]] = {
    "default": {},
    "high_recall": {"reconcile_candidate_limit": 128},
}
PRESET_NAMES = tuple(PRESETS.keys())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MEMHUB_",
        extra="forbid",
    )

    # Both modes append System1 facts/profiles using Jev decisions.
    # dual adds explicit digest for cross-evidence L3 cognition.
    mode: Literal["system1", "dual"] = "system1"
    storage_dir: str = "./.memhub_data"

    # Tuning preset: pick ONE named scenario instead of hand-tuning the advanced knobs
    # below. The preset only fills in fields you did NOT set explicitly (init/env/YAML
    # override it). See ``PRESETS`` for what each one changes.
    #   default     — balanced general SDK usage (== code defaults).
    #   high_recall — expand Jev material budget; preserve normal relation decisions.
    preset: Literal["default", "high_recall"] = "default"

    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    # Use OpenAI JSON mode (response_format=json_object) for JSON-returning calls.
    # Turn off only if the endpoint does not support response_format.
    llm_json_mode: bool = True
    # Provider-specific body (Volces thinking depth, vendor extensions, etc.).
    extra_body: dict[str, Any] = {}
    # Provider-specific headers (internal platform auth, tracing, etc.).
    extra_headers: dict[str, str] = {}
    llm_timeout: int = 60
    # Model context window (total tokens). Used with completion_reserve to derive
    # per-call char budget; long prompts are split into chunks + merged (not truncated).
    llm_context_window: int = LLM_CONTEXT_WINDOW
    llm_completion_reserve: int = LLM_COMPLETION_RESERVE
    chars_per_token: float = CHARS_PER_TOKEN

    embed_base_url: str = "https://api.openai.com/v1"
    embed_api_key: str = ""
    embed_model: str = "text-embedding-3-small"
    # Vector dimension — set to your embedding model's native output size (e.g. 1536, 1024).
    embed_dim: int = 1536
    embed_timeout: int = 30
    # Max input tokens per embed API call; longer texts are head-truncated (L1 anchors only).
    embed_max_tokens: int = EMBED_MAX_TOKENS
    embed_chars_per_token: float = EMBED_CHARS_PER_TOKEN

    auth_disabled: bool = True
    app_whitelist: list[str] = ["default"]
    # Default tenant namespace when add/list/search omit app_id (single-product default).
    default_app_id: str = "default"

    # Hub 入库前的自动 Secret 脱敏（OpenAI / GitHub / AWS / JWT / URL 密码等）。
    # 默认关闭：这是个人助理，用户说「记住我的 key」时就该记住。想防止工具片段里的
    # 密钥被长期保存，设 MEMHUB_REDACT_SECRETS=true。``<private>`` 标注与 Base64
    # 图片清洗始终生效，与本开关无关。
    redact_secrets: bool = False

    # Optional full serialization; default overlaps model I/O and validates scope CAS.
    write_serialize_per_user: bool = False

    # Typed decision service. Default endpoint is the public TypeSafe System One API.
    jev_endpoint: str = "https://api.typesafe.ai/v1/systemone"
    jev_api_key: str = ""
    jev_model: str = "jev-1.13.0"
    jev_timeout: float = 30.0
    jev_max_questions: int = 64
    jev_max_request_chars: int = 100_000
    jev_max_question_chars: int = 60_000
    reconcile_concurrency: int = 4
    reconcile_candidate_limit: int = 64
    reconcile_batch_size: int = 1
    # Relation routing: a candidate is compared (one Jev target question) only with its
    # own nearest current heads / earlier candidates at or above this cosine; with none
    # it is appended directly without a model call.
    reconcile_target_limit: int = 5
    reconcile_relation_threshold: float = 0.6
    # Relations Jev chose below this confidence are written as an independent
    # SUPPLEMENT immediately, without later review. 0 disables this confidence route.
    reconcile_review_confidence: float = 0.0
    # System 2 (hy-memory 2.0.4 aligned). Fresh current facts are clustered by
    # average-linkage cosine distance; clusters larger than system2_max_prompt_facts are
    # re-split at system2_refine_distance; near-duplicates (>= system2_dedup_cosine) are
    # collapsed; clusters smaller than system2_min_cluster_size are not induced.
    system2_cluster_distance: float = 0.35
    system2_refine_distance: float = 0.25
    system2_min_cluster_size: int = 4
    system2_dedup_cosine: float = 0.92
    system2_max_prompt_facts: int = 12
    # A schema must cite user-owned evidence spanning this many distinct raw sources.
    system2_min_sources: int = 2
    # Nearest existing schemas shown per cluster, so support is added instead of duplicated.
    system2_existing_per_group: int = 8
    system2_max_tokens: int = 4000
    # A new schema this cosine-close to an existing one that shares evidence is
    # converted to add_evidence.
    system2_schema_dedup_cosine: float = 0.68
    # Cross-domain sweeper: behaviour-space vs content-space collision.
    system2_cross_domain_enabled: bool = True
    system2_cross_domain_min_basics: int = 5
    system2_collision_beh_threshold: float = 0.70
    system2_collision_sem_threshold: float = 0.60
    # Question-blind persona catalog: exact current facts selected per domain.
    system2_catalog_enabled: bool = True
    # Fewer current facts than this: no catalog (a profile needs material to balance).
    system2_catalog_min_facts: int = 8
    system2_catalog_max_facts: int = 120
    system2_catalog_content_chars: int = 180
    system2_catalog_per_domain: int = 4
    system2_catalog_total: int = 32
    # Idempotent add(request_id=...) outcomes (chat and procedure decisions) are kept this
    # many seconds and purged by digest() (0 = forever).
    request_outcome_ttl_seconds: int = 7 * 24 * 3600

    # Legacy generic extractor cap in tokens (0 = disabled → rely on the LLM chunk+merge path).
    # Applies to the final content of BOTH content= and messages= (after history shaping). This
    # is the last-resort size cap (e.g. when user turns alone are huge); it is layered ON TOP of
    # — and does not conflict with — the role-aware history shaping below.
    extract_max_content_tokens: int = 0
    # Few-shot 示例：extract prompt 末尾追加示例，引导 4B 模型稳定格式。
    extract_few_shot_enabled: bool = False
    # Multi-turn extract input shaping (messages=...): no user/assistant turn is ever dropped.
    # When the dialogue exceeds ``extract_dialogue_context_ratio`` of ``llm_context_window``,
    # only assistant turns are truncated to ``extract_assistant_max_tokens``; user turns stay
    # full (primary memory signal). role=system dialogue turns are ignored (not memory).
    # The extract LLM's EXTRACT_* template system prompt is instruction-only, not chat history.
    extract_dialogue_context_ratio: float = 0.7
    extract_assistant_max_tokens: int = 512

    # Embedding write-side batching window for embed_queued (does not affect search-side embed).
    embed_queue_batch_size: int = 32
    embed_queue_window_ms: float = 200.0
    embed_cache_size: int = 10_000
    # Append-only history.db audit log (ADD/SUPERSEDE/DELETE snapshots). Off by default —
    # not used on the read path; enable for compliance / debugging.
    persist_history: bool = False
    # Drop reconcile/s2 queue rows after digest drains them (keeps cache.db small).
    purge_done_queues: bool = True

    # Hybrid read fusion: semantic vs in-pool BM25 weight (the sum need not be 1).
    hybrid_w_sem: float = 0.6
    hybrid_w_bm25: float = 0.4
    normal_candidate_multiplier: float = 3.0
    # Superseded memories near a query are followed to the version that replaced them,
    # so a change is recalled even when the query only resembles the older wording.
    # 0 disables the bridge.
    version_bridge_limit: int = 20
    # Schemas (basic + core) returned in their own group, after the evidence ``limit``
    # (never in place of System 1 memories). 0 returns none. Default 0: on PersonaMem-v2
    # (c20, 476 q, same stores) reading schemas cost -1.5pp (5W/12L) while the persona
    # catalog gained +1.7pp (19W/11L); schemas are still written by digest.
    derived_context_limit: int = 0
    # Forward route: schemas whose content is at least this cosine-similar to the query.
    schema_forward_min_score: float = 0.4
    # Reverse route: schemas explained by at least this many retrieved memories.
    schema_min_support: int = 1
    # Return the persona catalog (question-blind, exact current facts) with every search.
    catalog_context_enabled: bool = True
    # Coding scene: a write whose messages carry a tool trace (role="tool" or
    # assistant tool_calls) and that JEV judges to be engineering work also yields
    # procedure memories (L1, kind="procedure") next to its chat memories.
    procedure_enabled: bool = True
    # A coding session's dialogue (user requests + the answers they read) is
    # bounded to this many chars before procedure extraction and decisions;
    # user text is kept first. Leaves room for drafts and heads inside
    # ``jev_max_question_chars``.
    procedure_source_max_chars: int = 24_000
    # Hub prompt-hook progressive disclosure (experimental calibration knobs):
    # shared char/line cap across project view + dynamic recall + L0 excerpts,
    # and score threshold above which procedures expand their full solution.
    hub_inject_max_chars: int = 3_000
    hub_inject_max_lines: int = 120
    hub_procedure_full_score: float = 0.40

    @field_validator(
        "jev_timeout",
        "jev_max_questions",
        "jev_max_request_chars",
        "jev_max_question_chars",
        "reconcile_concurrency",
        "reconcile_candidate_limit",
        "reconcile_batch_size",
        "system2_max_prompt_facts",
        "system2_min_sources",
        "system2_cluster_distance",
        "system2_refine_distance",
        "system2_min_cluster_size",
        "system2_dedup_cosine",
        "system2_max_tokens",
        "system2_catalog_max_facts",
        "system2_catalog_per_domain",
        "system2_catalog_total",
    )
    @classmethod
    def _positive_decision_limit(cls, value: int | float) -> int | float:
        if value <= 0:
            raise ValueError("decision and cognition limits must be positive")
        return value

    @field_validator("request_outcome_ttl_seconds", "hub_inject_max_chars", "hub_inject_max_lines")
    @classmethod
    def _non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("must be non-negative")
        return value

    @field_validator("reconcile_review_confidence", "hub_procedure_full_score")
    @classmethod
    def _probability(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("probability/score threshold must be within [0, 1]")
        return value

    @field_validator("reconcile_candidate_limit")
    @classmethod
    def _choice_material_limit(cls, value: int) -> int:
        if value > 220:
            raise ValueError("candidate limit must be <=220 to fit Jev choice options")
        return value

    @field_validator("reconcile_batch_size")
    @classmethod
    def _proposal_limit(cls, value: int) -> int:
        if value > 24:
            raise ValueError("batch size must be <=24")
        return value

    @field_validator("app_whitelist", mode="before")
    @classmethod
    def _split_whitelist(cls, v):
        """Accept a comma-separated string for app_whitelist and split it into a list."""
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @field_validator("llm_context_window")
    @classmethod
    def _llm_context_window_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("llm_context_window must be positive")
        return v

    @field_validator("llm_completion_reserve")
    @classmethod
    def _llm_completion_reserve_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("llm_completion_reserve must be >= 0")
        return v

    @field_validator("chars_per_token")
    @classmethod
    def _chars_per_token_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("chars_per_token must be positive")
        return v

    @field_validator("embed_max_tokens")
    @classmethod
    def _embed_max_tokens_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("embed_max_tokens must be positive")
        return v

    @field_validator("embed_chars_per_token")
    @classmethod
    def _embed_chars_per_token_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("embed_chars_per_token must be positive")
        return v

    @property
    def llm_input_max_chars(self) -> int:
        """Estimated prompt char budget from context window minus completion reserve."""
        usable_tokens = self.llm_context_window - self.llm_completion_reserve
        if usable_tokens <= 0:
            return 0
        return int(usable_tokens * self.chars_per_token)

    @property
    def embed_input_max_chars(self) -> int:
        """Char budget of one embedding input, from a deliberately low ratio."""
        return int(self.embed_max_tokens * self.embed_chars_per_token)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Order config sources as init args > env vars > YAML file > preset > code defaults.

        The preset source sits at the LOWEST precedence (just above hard-coded field
        defaults): it only fills fields the user left unset via init/env/YAML. The preset
        NAME itself is resolved from those same three sources (in that order) so users can
        select a preset from anywhere.
        """
        ensure_config_file()
        yaml_source = YamlConfigSettingsSource(settings_cls, yaml_file=config_path())
        preset_source = _PresetSettingsSource(
            settings_cls, init_settings, env_settings, yaml_source
        )
        return (init_settings, env_settings, yaml_source, preset_source)

    @property
    def enable_graph(self) -> bool:
        """Whether the graph store is enabled (``dual`` mode only)."""
        return self.mode == "dual"

    @classmethod
    def from_dict(cls, config_dict: dict[str, Any]) -> Settings:
        """Build Settings from canonical flat keys; reject unknown keys."""
        return cls(**config_dict)


class _PresetSettingsSource(PydanticBaseSettingsSource):
    """Lowest-precedence source that injects a tuning preset's field values.

    The preset NAME is resolved from the higher-precedence sources (init > env > YAML);
    the resolved preset's dict is then returned so pydantic-settings treats those values
    as defaults — any field the user set explicitly elsewhere still wins.
    """

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        init_source: PydanticBaseSettingsSource,
        env_source: PydanticBaseSettingsSource,
        yaml_source: PydanticBaseSettingsSource,
    ) -> None:
        super().__init__(settings_cls)
        self._sources = (init_source, env_source, yaml_source)

    def _resolve_preset_name(self) -> str:
        """Read ``preset`` from init/env/YAML in precedence order; fall back to default."""
        for source in self._sources:
            try:
                values = source()
            except Exception:
                continue
            name = values.get("preset")
            if name:
                return str(name)
        return "default"

    def get_field_value(self, field, field_name):  # pragma: no cover - interface stub
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        name = self._resolve_preset_name()
        preset = PRESETS.get(name)
        if preset is None:
            valid = ", ".join(PRESET_NAMES)
            raise ValueError(f"Unknown preset {name!r}. Valid presets: {valid}.")
        return dict(preset)
