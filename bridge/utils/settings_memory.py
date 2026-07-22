"""Memory lifecycle settings domain (#584 P2-3).

Curated-memory policy, audience isolation, prompt identity labels, and the
Codex memory materializer configuration are extracted verbatim from
``utils/config.py``. ``Config`` inherits this plain mixin, so pydantic keeps
the same field names, aliases, defaults, and validation order.
"""

from pathlib import Path
import re
from typing import Literal, Optional

from pydantic import Field, field_validator, model_validator

from telegram_bot.utils.memory_policy import (
    assert_memory_provider_safe,
    assert_memory_scope_safe,
)


class MemorySettingsMixin:
    """Memory source, lifecycle, and audience-isolation configuration."""

    node_isolation_profile: Literal["fleet", "external"] = Field(
        default="fleet",
        alias="CCC_NODE_ISOLATION_PROFILE",
        description="Root policy inherited by Claude memory hooks.",
    )
    wiki_memory_enabled: bool = Field(
        default=True,
        alias="CCC_WIKI_MEMORY_ENABLED",
        description="Family Wiki memory source/sink toggle; external profile always overrides off.",
    )
    memory_user_label: str = Field(
        default="Seo Jin On / 서진원",
        alias="CCC_MEMORY_USER_LABEL",
        description="Prompt-only user identity label for memory injection/distill.",
    )
    memory_assistant_label: str = Field(
        default="dungae, a Hermes Team2 worker",
        alias="CCC_MEMORY_ASSISTANT_LABEL",
        description="Prompt-only assistant identity label for memory distill.",
    )
    codex_memory_materializer_path: str = Field(
        default_factory=lambda: str(Path.home() / ".claude" / "hooks" / "ccc_codex_memory.py"),
        alias="CCC_CODEX_MEMORY_MATERIALIZER_PATH",
        description="Body-free Codex memory materializer path.",
    )
    codex_memory_bootstrap_timeout_seconds: float = Field(
        default=14.0,
        ge=0.1,
        le=30.0,
        alias="CCC_CODEX_MEMORY_BOOTSTRAP_TIMEOUT_SEC",
        description="Timeout for each Codex memory materialize/status command.",
    )
    codex_audience_auth_mode: Literal["disabled", "keyring"] = Field(
        default="disabled",
        alias="CCC_CODEX_AUDIENCE_AUTH_MODE",
        description=(
            "Credential source for audience-scoped Codex homes. Keyring is the "
            "only supported activation mode; file credentials are never copied."
        ),
    )
    bridge_memory_mode: Literal["off", "curated", "audience-scoped"] = Field(
        default="off",
        alias="CCC_BRIDGE_MEMORY_MODE",
        description=(
            "Opt-in bridge memory lifecycle. curated loads only ccc-node memory/distill "
            "hooks through flag settings while filesystem setting sources stay disabled; "
            "audience-scoped keeps group/channel memory shared while DM memory stays private."
        ),
    )
    bridge_unsafe_shared_all_memory: bool = Field(
        default=False,
        alias="CCC_BRIDGE_UNSAFE_SHARED_ALL_MEMORY",
        description=(
            "Explicit unsafe compatibility override for legacy curated memory with "
            "shared-all. It never permits audience-scoped mode with shared-all."
        ),
    )
    bridge_memory_audience_root: Optional[Path] = Field(
        default=None,
        alias="CCC_BRIDGE_MEMORY_AUDIENCE_ROOT",
        description="Optional private root for audience-scoped bridge memory stores.",
    )
    bridge_memory_audience_key_path: Optional[Path] = Field(
        default=None,
        alias="CCC_BRIDGE_MEMORY_AUDIENCE_KEY_PATH",
        description="Optional local 0600 HMAC key path for opaque DM memory scopes.",
    )
    codex_distill_checkpoint_turns: int = Field(
        default=0,
        ge=0,
        le=1_000,
        alias="CCC_CODEX_DISTILL_CHECKPOINT_TURNS",
        description=(
            "Completed Codex turns before a checkpoint journal trigger; 0 disables "
            "this gate. The first enabled turn/byte/age gate reached triggers."
        ),
    )
    codex_distill_checkpoint_bytes: int = Field(
        default=0,
        ge=0,
        le=16 * 1024 * 1024,
        alias="CCC_CODEX_DISTILL_CHECKPOINT_BYTES",
        description=(
            "UTF-8 user plus assistant bytes before a checkpoint journal trigger; "
            "0 disables this gate."
        ),
    )
    codex_distill_checkpoint_age_seconds: int = Field(
        default=0,
        ge=0,
        le=7 * 24 * 60 * 60,
        alias="CCC_CODEX_DISTILL_CHECKPOINT_AGE_SECONDS",
        description=(
            "Runtime seconds since the prior checkpoint boundary; 0 disables this "
            "gate. Age is evaluated after a completed Codex turn."
        ),
    )
    codex_distill_model: str = Field(
        default="provider-default",
        alias="CCC_CODEX_DISTILL_MODEL",
        description=(
            "Isolated Codex distill model label. provider-default preserves the "
            "Codex CLI default; another safe model ID is passed explicitly."
        ),
    )
    codex_distill_timeout_seconds: float = Field(
        default=120.0,
        ge=1.0,
        le=600.0,
        alias="CCC_CODEX_DISTILL_TIMEOUT_SEC",
        description="Bounded timeout for one isolated Codex distill provider call.",
    )

    def hook_policy_environment(self) -> dict[str, str]:
        """Return validated, non-secret policy fields inherited by Claude hooks."""
        profile = self.node_isolation_profile
        return {
            "CCC_NODE_ISOLATION_PROFILE": profile,
            "CCC_WIKI_MEMORY_ENABLED": (
                "0" if profile == "external" else ("1" if self.wiki_memory_enabled else "0")
            ),
            "CCC_MEMORY_USER_LABEL": self.memory_user_label,
            "CCC_MEMORY_ASSISTANT_LABEL": self.memory_assistant_label,
        }

    @field_validator("memory_user_label", "memory_assistant_label", mode="before")
    @classmethod
    def validate_memory_label(cls, v):
        value = " ".join(str(v).split())[:80]
        if not value:
            raise ValueError("memory identity labels must be non-empty")
        return value

    @field_validator("codex_memory_materializer_path", mode="before")
    @classmethod
    def validate_codex_memory_materializer_path(cls, v):
        value = str(v).strip()
        if not value:
            raise ValueError("Codex runtime paths must be non-empty")
        return value

    @field_validator("codex_distill_model", mode="before")
    @classmethod
    def validate_codex_distill_model(cls, v):
        if not isinstance(v, str):
            raise ValueError("CCC_CODEX_DISTILL_MODEL must be a safe model identifier")
        value = v.strip()
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value) is None:
            raise ValueError("CCC_CODEX_DISTILL_MODEL must be a safe model identifier")
        return value

    @model_validator(mode="after")
    def validate_bridge_memory_scope(self):
        assert_memory_scope_safe(
            self.bridge_memory_mode,
            self.telegram_session_scope,
            unsafe_shared_all_override=self.bridge_unsafe_shared_all_memory,
        )
        assert_memory_provider_safe(
            self.bridge_memory_mode,
            self.agent_provider,
            self.codex_audience_auth_mode,
        )
        return self
