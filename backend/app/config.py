from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    environment: str = "development"
    database_url: str | None = None
    postgres_schema: str = "invoice_breakdown"
    cors_allowed_origins: str = "http://localhost:5173"
    llm_api_key: SecretStr | None = None
    classification_model: str | None = None
    classification_skill_path: Path | None = None
    classification_codex_path: Path | None = None
    operator_tokens: SecretStr | None = None
    source_storage_directory: Path | None = None
    azure_client_id: str | None = None
    azure_storage_blob_endpoint: str | None = None
    azure_storage_documents_container: str = "documents"
    azure_storage_processed_documents_container: str = "processed-documents"

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )


settings = Settings()
