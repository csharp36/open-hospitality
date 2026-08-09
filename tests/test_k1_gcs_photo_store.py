"""K1: the GCS-backed punch-photo store (Pillar K, decision 4).

Cloud Run filesystems are ephemeral and per-container: the seed job's
container is not the serving container, so face photos must land in
shared durable storage or they never reach the API at all. The GCS
store is a drop-in behind the existing `PhotoStore` port with the
LocalPhotoStore contract intact: AES-256-GCM app-side encryption — the
bucket only ever sees ciphertext, and a bucket-level leak discloses
nothing without the field key.

Tests run against a fake of the google-cloud-storage client surface the
store uses; no network, no credentials (the client is built lazily so
constructing the store — as create_app does — needs neither).
"""

import pytest
from google.api_core.exceptions import NotFound

from usali.crypto import decrypt_bytes
from usali.db import make_session_factory
from usali.gcs_photo_store import GcsPhotoStore
from usali.photo_store import LocalPhotoStore, PhotoNotFound, face_reference_key
from usali.server import create_app


class FakeBlob:
    def __init__(self, objects: dict, name: str) -> None:
        self._objects = objects
        self._name = name

    def upload_from_string(self, data, content_type=None):
        self._objects[self._name] = bytes(data)

    def download_as_bytes(self) -> bytes:
        if self._name not in self._objects:
            raise NotFound(f"blob {self._name}")
        return self._objects[self._name]

    def delete(self) -> None:
        if self._name not in self._objects:
            raise NotFound(f"blob {self._name}")
        del self._objects[self._name]


class FakeBucket:
    def __init__(self, objects: dict) -> None:
        self.objects = objects

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self.objects, name)


class FakeClient:
    def __init__(self) -> None:
        self.buckets: dict[str, dict] = {}

    def bucket(self, name: str) -> FakeBucket:
        return FakeBucket(self.buckets.setdefault(name, {}))


_JPEG = b"\xff\xd8\xff" + b"synthetic-star face bytes" * 20


def _store() -> tuple[GcsPhotoStore, FakeClient]:
    client = FakeClient()
    return GcsPhotoStore("demo-photos", client=client), client


def test_round_trips_and_the_bucket_sees_only_ciphertext():
    """The LocalPhotoStore contract: encrypt on put, decrypt on get. The
    RAW bucket object must be ciphertext — neither equal to the photo
    nor containing it — while still decrypting back with the field key
    (so the assertion cannot pass on scrambled-but-not-encrypted data,
    and a mutant that stores plaintext dies here twice)."""
    store, client = _store()
    # Org 1's photo key IS the raw master key (L5): an org-1 object's
    # ciphertext decrypts with decrypt_bytes (the master-key path) exactly
    # as before L5 — the org-1 compatibility pin at the store boundary.
    key = face_reference_key(1, 9001)
    store.put(key, _JPEG)

    raw = client.buckets["demo-photos"][key]
    assert raw != _JPEG
    assert _JPEG not in raw
    assert decrypt_bytes(raw) == _JPEG

    assert store.get(key) == _JPEG


def test_get_missing_raises_photo_not_found():
    """The port's error, not the GCS library's: callers already catch
    PhotoNotFound (B2's read endpoint) and must not learn a new
    exception type because the deployment changed stores."""
    store, _ = _store()
    with pytest.raises(PhotoNotFound):
        store.get("face-reference/404.jpg")


def test_delete_removes_and_is_idempotent():
    """Termination deletes by derived key without checking existence
    first (F3) — a second delete must be a no-op, exactly
    LocalPhotoStore's missing_ok semantics."""
    store, _ = _store()
    store.put("face-reference/9002.jpg", _JPEG)
    store.delete("face-reference/9002.jpg")
    with pytest.raises(PhotoNotFound):
        store.get("face-reference/9002.jpg")
    store.delete("face-reference/9002.jpg")  # idempotent, no raise


def test_construction_is_lazy_no_client_no_credentials():
    """create_app constructs the store at startup; Cloud Run injects
    credentials via its metadata service AT REQUEST TIME. Construction
    must therefore touch neither network nor credential lookup — this
    environment has no GCP credentials, so an eager client would
    raise."""
    store = GcsPhotoStore("demo-photos")
    assert store.bucket_name == "demo-photos"


def test_create_app_selects_gcs_when_the_bucket_is_configured(
    db_engine, tmp_path, monkeypatch
):
    """The crm_provider selection pattern: empty setting = the local
    store (dev/pilot default, unchanged); a configured bucket = GCS.
    Pinned at create_app so the deployment switch is one env var."""
    monkeypatch.setenv("USALI_PHOTO_STORE_GCS_BUCKET", "demo-photos")
    app = create_app(
        inbox_dir=tmp_path / "inbox", processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
    )
    assert isinstance(app.state.photo_store, GcsPhotoStore)
    assert app.state.photo_store.bucket_name == "demo-photos"

    monkeypatch.delenv("USALI_PHOTO_STORE_GCS_BUCKET")
    app = create_app(
        inbox_dir=tmp_path / "inbox2", processed_dir=tmp_path / "processed2",
        failed_dir=tmp_path / "failed2",
        session_factory=make_session_factory(db_engine),
    )
    assert isinstance(app.state.photo_store, LocalPhotoStore)


def test_the_selector_is_one_function(monkeypatch):
    """photo_store_from_settings IS the selection (one predicate, one
    function): bucket set -> GCS, empty -> local encrypted files."""
    from usali.config import get_settings
    from usali.photo_store import photo_store_from_settings

    monkeypatch.setenv("USALI_PHOTO_STORE_GCS_BUCKET", "demo-photos")
    get_settings.cache_clear() if hasattr(get_settings, "cache_clear") else None
    store = photo_store_from_settings(get_settings())
    assert isinstance(store, GcsPhotoStore)

    monkeypatch.delenv("USALI_PHOTO_STORE_GCS_BUCKET")
    if hasattr(get_settings, "cache_clear"):
        get_settings.cache_clear()
    assert isinstance(photo_store_from_settings(get_settings()), LocalPhotoStore)


def test_the_seed_writes_faces_through_the_selected_store():
    """The wiring pin (the K1 catch): _seed_faces once built
    LocalPhotoStore directly, so a cloud seed job would write photos to
    its own throwaway disk while the API read an empty bucket. The seed
    must go through photo_store_from_settings and never name a concrete
    store again."""
    import importlib.util
    import inspect
    from pathlib import Path

    script = Path(__file__).parent.parent / "scripts" / "demo_seed.py"
    spec = importlib.util.spec_from_file_location("demo_seed_k1_pin", script)
    assert spec is not None and spec.loader is not None
    demo_seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo_seed)

    src = inspect.getsource(demo_seed._seed_faces)
    assert "photo_store_from_settings" in src
    assert "LocalPhotoStore" not in src
