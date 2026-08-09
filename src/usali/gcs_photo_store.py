"""GCS-backed punch-photo store (Pillar K1, plan decision 4).

The deployment drop-in the `photo_store` module always promised: same
`PhotoStore` protocol, same contract as `LocalPhotoStore` — AES-256-GCM
app-side encryption via the field key, so the bucket only ever sees
ciphertext and a bucket-level leak discloses nothing without it. Bucket
hygiene (uniform bucket-level access, no public ACLs, the app service
account as the only principal) is pinned in the K2 bootstrap, not here.

Why it exists: Cloud Run filesystems are ephemeral and per-container.
The migrate-seed job's container is not the serving container, so the
demo's synthetic-star face photos must land in shared durable storage
or the API could never read them.

The client is built LAZILY: `create_app` constructs the store at
startup, and Cloud Run resolves credentials through its metadata
service — construction must touch neither network nor credentials
(tests construct freely with no GCP environment at all).
"""

from typing import Any

from google.api_core.exceptions import NotFound

from usali.crypto import decrypt_photo, encrypt_photo
from usali.photo_store import PhotoNotFound, org_from_key


class GcsPhotoStore:
    def __init__(self, bucket_name: str, client: Any | None = None) -> None:
        self.bucket_name = bucket_name
        self._client = client

    def _bucket(self) -> Any:
        if self._client is None:
            # No py.typed marker upstream; the client is used as Any
            # through the two-method surface the fakes mirror.
            from google.cloud import storage  # type: ignore[import-untyped]

            self._client = storage.Client()
        return self._client.bucket(self.bucket_name)

    def put(self, key: str, data: bytes) -> None:
        self._bucket().blob(key).upload_from_string(
            encrypt_photo(data, org_from_key(key)),
            content_type="application/octet-stream",
        )

    def get(self, key: str) -> bytes:
        try:
            blob = self._bucket().blob(key).download_as_bytes()
        except NotFound:
            raise PhotoNotFound(key) from None
        return decrypt_photo(blob, org_from_key(key))

    def delete(self, key: str) -> None:
        # Idempotent like LocalPhotoStore's missing_ok: termination
        # deletes by derived key without checking existence first (F3).
        try:
            self._bucket().blob(key).delete()
        except NotFound:
            pass
