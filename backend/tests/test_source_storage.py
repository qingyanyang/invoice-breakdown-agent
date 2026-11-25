from pathlib import Path
from uuid import uuid4

import pytest

from app.source_cleanup import SOURCE_KEY
from app.source_storage import LocalSourceStorage, SourceAlreadyExists


def test_staging_cleanup_failure_preserves_collision(tmp_path, monkeypatch, caplog):
    directory = tmp_path / "relative-source-storage"
    monkeypatch.chdir(tmp_path)
    storage = LocalSourceStorage(Path(directory.name))
    key = f"sources/{uuid4()}/{uuid4()}.xlsx"
    storage.put(key, b"first source")
    original_unlink = Path.unlink

    def fail_staging_unlink(path, *args, **kwargs):
        if path.suffix == ".staging":
            raise PermissionError("private staging path")
        original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_staging_unlink)
        with pytest.raises(SourceAlreadyExists):
            storage.put(key, b"must not overwrite")

    assert storage.read(key, 12) == b"first source"
    staged, = directory.rglob("*.staging")
    staging_key = staged.relative_to(directory).as_posix()
    assert SOURCE_KEY.fullmatch(staging_key)["staging"]
    assert f"Source staging cleanup pending for key {staging_key}" in caplog.text


def test_azure_uses_separate_configured_output_container(monkeypatch):
    from app import source_storage
    from app.config import Settings

    class BlobClient:
        def __init__(self, _endpoint, **_options):
            pass

        def get_container_client(self, name):
            return name

    monkeypatch.setattr(source_storage, "BlobServiceClient", BlobClient)
    monkeypatch.setattr(source_storage, "ManagedIdentityCredential", lambda **_options: object())
    config = Settings(
        _env_file=None, azure_storage_blob_endpoint="https://example.invalid",
        azure_storage_documents_container="input-container",
        azure_storage_processed_documents_container="output-container",
    )
    assert source_storage.create_source_storage(config).container == "input-container"
    assert source_storage.create_output_storage(config).container == "output-container"
