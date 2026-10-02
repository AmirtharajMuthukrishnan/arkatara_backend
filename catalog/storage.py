"""Provider-neutral object storage; no provider or upload limit is selected here."""

import re
from pathlib import Path
from typing import BinaryIO, Protocol

from django.conf import settings
from django.core.exceptions import ValidationError


class MediaStorageUnavailable(ValidationError):
    pass


KEY_PATTERN = re.compile(
    r"products/(?P<product>[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})/"
    r"(?P<asset>[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.(png|jpg|mp4)"
)


def validate_object_key(key: str) -> str:
    if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key):
        raise ValidationError("Invalid media object reference.")
    return key


class ObjectStorage(Protocol):
    name: str

    def put(self, key: str, content: bytes) -> None: ...

    def open(self, key: str) -> BinaryIO: ...

    def delete(self, key: str) -> None: ...


class LocalObjectStorage:
    name = "LOCAL"

    def __init__(self, root):
        # Local files are an explicit development adapter, never a production fallback.
        # Refs: DECISIONS.md D-14, BD-15; INTEGRATIONS.md#object-storage--task-3.
        if settings.ENVIRONMENT not in {"local", "test"}:
            raise MediaStorageUnavailable("Local media storage is restricted to local/test.")
        if not root or not Path(root).is_absolute():
            raise MediaStorageUnavailable("Configure an absolute dedicated local media directory.")
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        validate_object_key(key)
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise ValidationError("Media object reference escapes its configured storage.")
        return path

    def put(self, key: str, content: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation ensures failed uploads cannot overwrite/delete another asset.
        owned = False
        try:
            with path.open("xb") as output:
                owned = True
                output.write(content)
        except BaseException:
            if owned:
                path.unlink(missing_ok=True)
            raise

    def open(self, key: str) -> BinaryIO:
        return self._path(key).open("rb")

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


def get_media_storage() -> ObjectStorage:
    backend = getattr(settings, "PRODUCT_MEDIA_STORAGE", "UNCONFIGURED")
    if backend == "LOCAL":
        return LocalObjectStorage(getattr(settings, "PRODUCT_MEDIA_ROOT", None))
    raise MediaStorageUnavailable("Product media storage is not configured.")
