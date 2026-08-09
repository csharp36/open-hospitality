"""Punch-photo storage behind an injectable seam (Pillar B1).

Photos are face images of employees: encrypted at rest, never in the DB (the
`punch` row holds only a key), and purged on a retention clock after timecard
approval (B2). `PhotoStore` is injected into `create_app` exactly like
`TokenVerifier` (A1) and `KeycloakAdmin` (A2.3), so tests use
`InMemoryPhotoStore` — no filesystem, no S3, no camera in the test loop.

Production uses S3 + SSE-KMS; that `S3PhotoStore` is a deployment-time drop-in
against this same protocol and is deliberately not built here.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from usali.crypto import decrypt_photo, encrypt_photo
from usali.tenancy import FOUNDING_ORG_ID

if TYPE_CHECKING:
    from usali.config import Settings


# A punch photo is a JPEG: the kiosk refuses anything else at upload (B1), and
# the read endpoint refuses to *serve* anything else as an image (B2). Single
# source of truth so the two gates cannot drift apart.
JPEG_MAGIC = b"\xff\xd8\xff"

# The per-org key namespace (Pillar L decision 5): every object key is
# `org/<org_id>/<subkey>`. The prefix is BOTH the routing (which folder /
# bucket path the bytes land under) AND the crypto salt — `org_from_key`
# recovers <org_id> to derive the per-org photo key, so a prefix-routing
# bug and a decryption mismatch are the SAME event: one value, the design's
# intent. The store is a stateless singleton on `app.state`; the org rides
# IN the key rather than through the put/get/delete signature, so the key
# stays the single source of an object's tenant (the DB `punch.photo_key`
# carries it forward to the purge, the deterministic face key rebuilds it).
_ORG_KEY_PREFIX = "org"


class PhotoNotFound(Exception):
    """No photo stored under that key."""


def org_prefixed_key(org_id: int, subkey: str) -> str:
    """Namespace a photo subkey under its org: `org/<org_id>/<subkey>`.

    The `org_id` MUST come from the request's active org / trusted device
    binding (`current_org_id(session)`), never from client input — it is
    the crypto salt, so a caller-chosen org would let a client seal photos
    under (or route reads at) another tenant's key."""
    key = f"{_ORG_KEY_PREFIX}/{int(org_id)}/{subkey}"
    # The L6-fallback invariant, locked in AT the one construction point:
    # every produced key is `org/<int>/…` so `org_from_key` recovers a real
    # org — no write may ever emit an un-prefixed key (which would silently
    # fall back to the founding master key). This holds BEFORE L6 opens
    # multi-org write paths; the int() coercion above is what guarantees it.
    assert key.startswith(f"{_ORG_KEY_PREFIX}/{int(org_id)}/"), key
    return key


def org_from_key(key: str) -> int:
    """Recover the org a key was written under, for the per-org crypto key.

    A key WITHOUT the `org/<id>/` prefix is a PRE-L5 object: by construction
    it belongs to the founding org (org 1 — A2.1 seeded exactly one org),
    whose photo key is DEFINED as the raw master key, so those objects stay
    readable with no re-encrypt (crypto._photo_key). A malformed prefix
    falls back the same way rather than guessing a foreign org."""
    parts = key.split("/", 2)
    if len(parts) == 3 and parts[0] == _ORG_KEY_PREFIX:
        try:
            return int(parts[1])
        except ValueError:
            pass
    return FOUNDING_ORG_ID


def face_reference_key(org_id: int, employee_id: int) -> str:
    """The storage key for an employee's face-enrollment reference photo
    (F3), namespaced to the employee's org (L5). DETERMINISTIC on purpose:
    termination must be able to delete the photo without a pointer column —
    deriving the key is what guarantees the biometric's evidence photo
    can't be orphaned by a lost reference. `org_id` is the caller's active
    org (`current_org_id(session)`); the employee belongs to it by the
    walls, so put and delete rebuild the identical key."""
    return org_prefixed_key(org_id, f"face-reference/{employee_id}.jpg")


class PhotoStore(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...


def photo_store_from_settings(settings: "Settings") -> "PhotoStore":
    """THE config-selected default store — one predicate, one function:
    `create_app` and the demo seed must never each decide where photos
    live (a seed writing local files while the API reads a bucket is
    exactly the split this prevents, K1). A non-empty
    `photo_store_gcs_bucket` selects GCS (lazy client, app-side
    ciphertext); empty keeps the local encrypted-file store."""
    if settings.photo_store_gcs_bucket:
        # Local import: GCS deps stay out of the module graph for every
        # caller that never configures a bucket.
        from usali.gcs_photo_store import GcsPhotoStore

        return GcsPhotoStore(settings.photo_store_gcs_bucket)
    return LocalPhotoStore(settings.photo_store_dir)


class LocalPhotoStore:
    """Filesystem store, AES-256-GCM encrypted at rest. Dev/pilot default."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def _path(self, key: str) -> Path:
        path = (self._root / key).resolve()
        if not path.is_relative_to(self._root.resolve()):
            raise ValueError(f"photo key escapes the store root: {key!r}")
        return path

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encrypt_photo(data, org_from_key(key)))

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise PhotoNotFound(key)
        return decrypt_photo(path.read_bytes(), org_from_key(key))

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class InMemoryPhotoStore:
    """Offline fake for tests and the e2e harness."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    def put(self, key: str, data: bytes) -> None:
        self._blobs[key] = data

    def get(self, key: str) -> bytes:
        if key not in self._blobs:
            raise PhotoNotFound(key)
        return self._blobs[key]

    def delete(self, key: str) -> None:
        self._blobs.pop(key, None)

    def keys(self) -> set[str]:
        return set(self._blobs)
