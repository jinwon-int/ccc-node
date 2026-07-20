"""Voice/transcription settings domain (#584 P2-3).

Whisper and Volcengine bigmodel file-ASR configuration extracted verbatim
from ``utils/config.py``. ``Config`` inherits this mixin, so every field
name, env alias, default, and validator behaves exactly as before.

The mixin is intentionally a plain class (no pydantic base): pydantic v2
collects annotated fields and ``field_validator``/``model_validator``
decorators from non-model bases when building ``Config``, which keeps the
MRO simple and avoids merging multiple ``model_config`` definitions.

Standalone-config contract: this module must stay importable as a leaf of
the synthetic ``telegram_bot.utils`` package that
``tests/test_config_voice_provider.py`` builds in a fresh process, so it
may only import stdlib and pydantic.
"""

from typing import Optional

from pydantic import Field, field_validator, model_validator


class VoiceSettingsMixin:
    """Voice message configuration (Whisper + Volcengine ASR)."""

    transcription_provider: str = Field(
        default="whisper",
        description=("Voice transcription provider. Supported values: whisper, volcengine"),
    )
    openai_api_key: Optional[str] = Field(
        default=None, description="OpenAI API key used for Whisper transcription"
    )
    openai_base_url: Optional[str] = Field(
        default=None,
        description="Optional OpenAI-compatible API base URL for Whisper transcription",
    )
    whisper_model: str = Field(
        default="whisper-1", description="Whisper model name for voice transcription"
    )
    max_voice_duration: int = Field(
        default=300, description="Maximum accepted voice duration in seconds"
    )
    ffmpeg_path: Optional[str] = Field(
        default=None,
        description="Optional absolute path to ffmpeg binary (defaults to system PATH)",
    )
    voice_reply_persona: str = Field(
        default="Tingting",
        description="Default persona name for voice replies",
    )
    # Volcengine bigmodel file ASR fields (v3 submit/query)
    volcengine_app_id: Optional[str] = Field(
        default=None, description="Volcengine appid for bigmodel file ASR"
    )
    volcengine_token: Optional[str] = Field(
        default=None, description="Volcengine token for bigmodel file ASR"
    )
    volcengine_access_key: Optional[str] = Field(
        default=None, description="Volcengine Access Key for TOS upload"
    )
    volcengine_secret_access_key: Optional[str] = Field(
        default=None, description="Volcengine Secret Access Key for TOS upload"
    )
    volcengine_tos_bucket_name: Optional[str] = Field(
        default=None, description="Volcengine TOS bucket name for staging voice files"
    )
    volcengine_tos_endpoint: str = Field(default="", description="Volcengine TOS endpoint URL")
    volcengine_tos_region: str = Field(default="cn-beijing", description="Volcengine TOS region")
    volcengine_tos_signed_url_ttl_seconds: int = Field(
        default=900,
        description="Signed URL TTL in seconds for Volcengine ASR to fetch staged voice file",
    )
    volcengine_cluster: str = Field(
        default="volc_auc_common",
        description="Volcengine cluster (reserved for compatibility)",
    )
    volcengine_resource_id: str = Field(
        default="volc.bigasr.auc",
        description="Volcengine X-Api-Resource-Id for bigmodel file ASR",
    )
    volcengine_model_name: str = Field(
        default="bigmodel",
        description="Volcengine request.model_name for bigmodel file ASR",
    )
    volcengine_submit_endpoint: str = Field(
        default="https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit",
        description="Volcengine bigmodel ASR submit endpoint URL",
    )
    volcengine_query_endpoint: str = Field(
        default="https://openspeech.bytedance.com/api/v3/auc/bigmodel/query",
        description="Volcengine bigmodel ASR query endpoint URL",
    )
    volcengine_timeout_seconds: float = Field(
        default=20.0,
        description="Volcengine request timeout in seconds",
    )
    volcengine_max_retries: int = Field(
        default=3,
        description="Maximum retry attempts for Volcengine transcription",
    )
    volcengine_initial_backoff: float = Field(
        default=1.0,
        description="Initial retry backoff seconds for Volcengine transcription",
    )
    volcengine_poll_interval_seconds: float = Field(
        default=2.0,
        description="Polling interval in seconds for Volcengine query",
    )
    volcengine_max_poll_seconds: float = Field(
        default=300.0,
        description="Maximum polling duration in seconds for Volcengine query",
    )

    @field_validator("transcription_provider", mode="before")
    @classmethod
    def normalize_transcription_provider(cls, v):
        provider = str(v or "whisper").strip().lower()
        allowed = {"whisper", "volcengine"}
        if provider not in allowed:
            raise ValueError("TRANSCRIPTION_PROVIDER must be one of: whisper, volcengine.")
        return provider

    @field_validator("openai_api_key", mode="before")
    @classmethod
    def normalize_openai_key(cls, v):
        if v is None:
            return None
        value = str(v).strip()
        return value or None

    @field_validator("openai_base_url", mode="before")
    @classmethod
    def normalize_openai_base_url(cls, v):
        if v is None:
            return None
        value = str(v).strip()
        return value or None

    @field_validator("voice_reply_persona")
    @classmethod
    def normalize_voice_reply_text(cls, v):
        value = str(v or "").strip()
        if not value:
            raise ValueError("VOICE_REPLY_PERSONA must not be empty.")
        return value

    @field_validator(
        "volcengine_app_id",
        "volcengine_token",
        "volcengine_access_key",
        "volcengine_secret_access_key",
        "volcengine_tos_bucket_name",
        "volcengine_cluster",
        mode="before",
    )
    @classmethod
    def normalize_volcengine_secret(cls, v, info):
        if info.field_name == "volcengine_cluster":
            value = str(v or "").strip()
            return value or "volc_auc_common"
        if v is None:
            return None
        value = str(v).strip()
        return value or None

    @field_validator(
        "volcengine_submit_endpoint",
        "volcengine_query_endpoint",
        "volcengine_resource_id",
        "volcengine_model_name",
        "volcengine_tos_region",
    )
    @classmethod
    def validate_volcengine_required_text(cls, v, info):
        value = str(v).strip()
        if not value:
            env_name = info.field_name.upper()
            raise ValueError(f"{env_name} must not be empty.")
        return value

    @field_validator("max_voice_duration")
    @classmethod
    def validate_max_voice_duration(cls, v):
        if v <= 0:
            raise ValueError("MAX_VOICE_DURATION must be a positive integer.")
        return v

    @field_validator("volcengine_timeout_seconds")
    @classmethod
    def validate_volcengine_timeout_seconds(cls, v):
        if v <= 0:
            raise ValueError("VOLCENGINE_TIMEOUT_SECONDS must be positive.")
        return v

    @field_validator("volcengine_max_retries")
    @classmethod
    def validate_volcengine_max_retries(cls, v):
        if v <= 0:
            raise ValueError("VOLCENGINE_MAX_RETRIES must be a positive integer.")
        return v

    @field_validator("volcengine_initial_backoff")
    @classmethod
    def validate_volcengine_initial_backoff(cls, v):
        if v <= 0:
            raise ValueError("VOLCENGINE_INITIAL_BACKOFF must be positive.")
        return v

    @field_validator("volcengine_poll_interval_seconds")
    @classmethod
    def validate_volcengine_poll_interval_seconds(cls, v):
        if v <= 0:
            raise ValueError("VOLCENGINE_POLL_INTERVAL_SECONDS must be positive.")
        return v

    @field_validator("volcengine_max_poll_seconds")
    @classmethod
    def validate_volcengine_max_poll_seconds(cls, v):
        if v <= 0:
            raise ValueError("VOLCENGINE_MAX_POLL_SECONDS must be positive.")
        return v

    @field_validator("volcengine_tos_signed_url_ttl_seconds")
    @classmethod
    def validate_volcengine_tos_signed_url_ttl_seconds(cls, v):
        if v <= 0:
            raise ValueError("VOLCENGINE_TOS_SIGNED_URL_TTL_SECONDS must be positive.")
        return v

    @model_validator(mode="after")
    def validate_provider_specific_config(self):
        if self.transcription_provider != "volcengine":
            return self

        missing = []
        if not self.volcengine_app_id:
            missing.append("VOLCENGINE_APP_ID")
        if not self.volcengine_token:
            missing.append("VOLCENGINE_TOKEN")
        if not self.volcengine_access_key:
            missing.append("VOLCENGINE_ACCESS_KEY")
        if not self.volcengine_secret_access_key:
            missing.append("VOLCENGINE_SECRET_ACCESS_KEY")
        if not self.volcengine_tos_bucket_name:
            missing.append("VOLCENGINE_TOS_BUCKET_NAME")
        if not self.volcengine_tos_endpoint:
            missing.append("VOLCENGINE_TOS_ENDPOINT")
        if missing:
            raise ValueError(
                "Volcengine transcription provider requires: " + ", ".join(missing) + "."
            )
        return self
