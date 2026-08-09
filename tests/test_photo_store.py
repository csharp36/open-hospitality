import pytest

from usali.photo_store import InMemoryPhotoStore, LocalPhotoStore, PhotoNotFound

_JPEG = b"\xff\xd8\xff\xe0 fake jpeg bytes \x00\x01"


def test_local_store_round_trips(tmp_path):
    store = LocalPhotoStore(tmp_path)
    store.put("HISJ/abc.bin", _JPEG)
    assert store.get("HISJ/abc.bin") == _JPEG


def test_local_store_is_encrypted_at_rest(tmp_path):
    store = LocalPhotoStore(tmp_path)
    store.put("HISJ/abc.bin", _JPEG)
    raw = (tmp_path / "HISJ" / "abc.bin").read_bytes()
    assert raw != _JPEG
    assert b"fake jpeg bytes" not in raw  # plaintext must not survive on disk


def test_local_store_delete(tmp_path):
    store = LocalPhotoStore(tmp_path)
    store.put("HISJ/abc.bin", _JPEG)
    store.delete("HISJ/abc.bin")
    with pytest.raises(PhotoNotFound):
        store.get("HISJ/abc.bin")


def test_local_store_missing_key_raises(tmp_path):
    with pytest.raises(PhotoNotFound):
        LocalPhotoStore(tmp_path).get("nope.bin")


def test_in_memory_store_round_trips_and_deletes():
    store = InMemoryPhotoStore()
    store.put("k", _JPEG)
    assert store.get("k") == _JPEG
    assert store.keys() == {"k"}
    store.delete("k")
    with pytest.raises(PhotoNotFound):
        store.get("k")


def test_local_store_rejects_key_escaping_the_root(tmp_path):
    with pytest.raises(ValueError):
        LocalPhotoStore(tmp_path).put("../escape.bin", b"x")
