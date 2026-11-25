from pathlib import Path

from pydantic import Field, SecretStr
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
    qa_worker_image: str | None = None
    qa_docker_runtime: str = "docker"
    operator_tokens: SecretStr | None = None
    source_storage_directory: Path | None = None
    azure_client_id: str | None = None
    azure_storage_blob_endpoint: str | None = None
    azure_storage_documents_container: str = "documents"
    azure_storage_processed_documents_container: str = "processed-documents"

    execution_max_attempts: int = Field(default=3, ge=1, le=10)
    execution_retry_base_seconds: float = Field(default=0.5, gt=0, le=30)
    execution_lease_seconds: float = Field(default=30, ge=1, le=300)
    execution_deadline_seconds: int = Field(default=720, ge=1, le=3600)
    execution_duplicate_wait_seconds: float = Field(default=2, ge=0, le=10)
    execution_db_timeout_seconds: int = Field(default=5, ge=1, le=30)
    execution_upload_seconds: int = Field(default=45, ge=1, le=600)
    execution_checkpoint_seconds: int = Field(default=25, ge=1, le=600)
    execution_extraction_seconds: int = Field(default=25, ge=1, le=600)
    execution_classification_seconds: int = Field(default=60, ge=1, le=600)
    execution_rules_seconds: int = Field(default=25, ge=1, le=600)
    execution_workbook_seconds: int = Field(default=55, ge=1, le=600)
    execution_qa_seconds: int = Field(default=260, ge=1, le=600)

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )


settings = Settings()
