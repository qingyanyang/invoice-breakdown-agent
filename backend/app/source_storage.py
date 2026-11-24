import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Protocol

from azure.core.exceptions import AzureError, ResourceExistsError, ResourceNotFoundError
from azure.identity import ManagedIdentityCredential
from azure.storage.blob import BlobServiceClient

from app.config import Settings


class StorageError(RuntimeError):
    pass


class SourceAlreadyExists(StorageError):
    pass


class SourceStorage(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def read(self, key: str, size: int) -> bytes: ...
    def delete(self, key: str) -> None: ...
    def close(self) -> None: ...


class LocalSourceStorage:
    def __init__(self, directory: Path):
        self.directory = directory

    def put(self, key: str, data: bytes) -> None:
        destination = self.directory / key
        temporary = None
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                raise SourceAlreadyExists from None
        except OSError:
            raise StorageError from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def read(self, key: str, size: int) -> bytes:
        try:
            with (self.directory / key).open("rb") as handle:
                return handle.read(size + 1)
        except OSError:
            raise StorageError from None

    def delete(self, key: str) -> None:
        try:
            (self.directory / key).unlink(missing_ok=True)
        except OSError:
            raise StorageError from None

    def close(self) -> None:
        pass


class AzureSourceStorage:
    def __init__(self, config: Settings):
        self.credential = ManagedIdentityCredential(client_id=config.azure_client_id)
        self.service = BlobServiceClient(
            config.azure_storage_blob_endpoint,
            credential=self.credential,
            connection_timeout=5,
            read_timeout=15,
            retry_total=1,
        )
        self.container = self.service.get_container_client(config.azure_storage_documents_container)

    def put(self, key: str, data: bytes) -> None:
        try:
            self.container.upload_blob(key, data, overwrite=False)
        except ResourceExistsError:
            raise SourceAlreadyExists from None
        except AzureError:
            raise StorageError from None

    def read(self, key: str, size: int) -> bytes:
        try:
            return self.container.download_blob(key, offset=0, length=size + 1).readall()
        except AzureError:
            raise StorageError from None

    def delete(self, key: str) -> None:
        try:
            self.container.delete_blob(key)
        except ResourceNotFoundError:
            pass
        except AzureError:
            raise StorageError from None

    def close(self) -> None:
        self.service.close()
        self.credential.close()


def create_source_storage(config: Settings) -> SourceStorage | None:
    if config.azure_storage_blob_endpoint:
        return AzureSourceStorage(config)
    if config.source_storage_directory:
        return LocalSourceStorage(config.source_storage_directory)
    return None
