"""Where resumes and screenshots live: a private Supabase bucket, or a local folder in dev."""
import hashlib
from pathlib import Path

from storage3.exceptions import StorageApiError
from supabase import create_client

from . import config


class StorageError(Exception):
    """A file could not be saved or read."""


class LocalStorage:
    def __init__(self, root: Path):
        self.root = root

    def _path(self, key: str) -> Path:
        # Short flat names, because Windows refuses long paths and this folder can already be deep.
        return self.root / hashlib.sha256(key.encode()).hexdigest()[:20]

    def put(self, key: str, data: bytes, content_type: str) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self._path(key).write_bytes(data)
        except OSError as exc:
            raise StorageError(key) from exc

    def get(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except OSError as exc:
            raise StorageError(key) from exc

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class SupabaseStorage:
    def __init__(self, url: str, service_key: str, bucket: str):
        self.client = create_client(url, service_key)
        self.bucket = bucket
        self._ensure_private_bucket()

    def _ensure_private_bucket(self) -> None:
        try:
            bucket = self.client.storage.get_bucket(self.bucket)
        except StorageApiError:
            self.client.storage.create_bucket(self.bucket, options={"public": False})
            return
        if bucket.public:
            raise RuntimeError(f"Supabase bucket '{self.bucket}' is public. It holds resumes, so make it private.")

    def _files(self):
        return self.client.storage.from_(self.bucket)

    def put(self, key: str, data: bytes, content_type: str) -> None:
        try:
            self._files().upload(path=key, file=data, file_options={"content-type": content_type, "upsert": "true"})
        except StorageApiError as exc:
            raise StorageError(key) from exc

    def get(self, key: str) -> bytes:
        try:
            return self._files().download(key)
        except StorageApiError as exc:
            raise StorageError(key) from exc

    def delete(self, key: str) -> None:
        try:
            self._files().remove([key])
        except StorageApiError as exc:
            raise StorageError(key) from exc


_store = None


def store():
    global _store
    if _store is None:
        if config.SUPABASE_URL:
            _store = SupabaseStorage(config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY, config.STORAGE_BUCKET)
        else:
            _store = LocalStorage(config.DATA_DIR / "files")
    return _store
