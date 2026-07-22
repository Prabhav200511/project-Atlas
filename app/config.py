from functools import lru_cache

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="ATLAS_", extra="ignore", populate_by_name=True)

    environment: str = "local"
    log_level: str = "INFO"
    cors_origins: str = Field(default="http://localhost:3000", validation_alias=AliasChoices("FRONTEND_URL", "ATLAS_CORS_ORIGINS"))
    database_url: str = Field(default="postgresql+asyncpg://atlas:atlas@localhost:55432/atlas", validation_alias=AliasChoices("DATABASE_URL", "ATLAS_DATABASE_URL"))
    supabase_url: str | None = Field(default=None, validation_alias=AliasChoices("SUPABASE_URL", "ATLAS_SUPABASE_URL"))
    supabase_service_role_key: str | None = Field(default=None, repr=False, validation_alias=AliasChoices("SUPABASE_SERVICE_ROLE_KEY", "ATLAS_SUPABASE_SERVICE_ROLE_KEY"))
    qdrant_url: str = Field(default="http://localhost:6333", validation_alias=AliasChoices("QDRANT_URL", "ATLAS_QDRANT_URL"))
    qdrant_api_key: str | None = Field(default=None, repr=False, validation_alias=AliasChoices("QDRANT_API_KEY", "ATLAS_QDRANT_API_KEY"))
    gemini_api_key: str | None = Field(default=None, repr=False, validation_alias=AliasChoices("GEMINI_API_KEY", "ATLAS_GEMINI_API_KEY"))
    groq_api_key: str | None = Field(default=None, repr=False, validation_alias=AliasChoices("GROQ_API_KEY", "ATLAS_GROQ_API_KEY"))
    groq_model: str = Field(default="llama-3.3-70b-versatile", validation_alias=AliasChoices("GROQ_MODEL", "ATLAS_GROQ_MODEL"))
    jwt_secret_key: str | None = Field(default=None, repr=False, validation_alias=AliasChoices("JWT_SECRET_KEY", "ATLAS_JWT_SECRET_KEY"))
    embedding_dimensions: int = 1536
    qdrant_collection: str = "atlas_chunks"
    index_version: str = "2"
    dense_retrieval_limit: int = 20
    bm25_retrieval_limit: int = 20
    hybrid_retrieval_limit: int = 12
    rrf_dense_weight: float = Field(default=1.0, gt=0)
    rrf_bm25_weight: float = Field(default=1.0, gt=0)
    rerank_candidate_limit: int = Field(default=12, ge=1, le=50)
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    reranker_score_threshold: float = 0.15
    context_min_chunks: int = 5
    context_max_chunks: int = 8
    context_diversity_threshold: float = 0.82
    max_context_tokens: int = 4_000
    upload_dir: str = "./uploads"
    max_upload_bytes: int = 50 * 1024 * 1024
    min_pdf_text_chars: int = 80
    auto_create_schema: bool = False
    chat_model: str = Field(default="gemini-2.0-flash", validation_alias=AliasChoices("GEMINI_MODEL", "ATLAS_CHAT_MODEL"))
    rfi_similarity_threshold: float = 0.75
    graph_dir: str = "./graphs"

    @property
    def allowed_cors_origins(self) -> list[str]:
        return [origin.strip().rstrip("/") for origin in self.cors_origins.split(",") if origin.strip()]

    @field_validator("qdrant_api_key", "gemini_api_key", "groq_api_key", mode="before")
    @classmethod
    def blank_secret_is_unset(cls, value: str | None) -> str | None:
        return value or None


@lru_cache
def get_settings() -> Settings:
    return Settings()
