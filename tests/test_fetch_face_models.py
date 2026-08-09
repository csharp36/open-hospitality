"""F2: the model fetch script's verification posture.

The models are never committed — they arrive over the network, and the
sha256 pins are the only thing standing between "our own matching code"
(tier 2 of docs/reference/biometric-attendance.md) and running whatever a
compromised mirror serves. So the property under test is: NOTHING lands in
the model dir unless every hash matched, and a mismatch says which file
and both hashes.
"""

import hashlib
import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fetch_face_models.py"
_spec = importlib.util.spec_from_file_location("fetch_face_models", _SCRIPT)
assert _spec is not None and _spec.loader is not None
fetch_face_models = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fetch_face_models)


def test_a_matching_hash_passes(tmp_path):
    f = tmp_path / "model.onnx"
    f.write_bytes(b"weights")
    fetch_face_models.verify_sha256(
        f, hashlib.sha256(b"weights").hexdigest(), label="model.onnx"
    )


def test_a_mismatched_hash_refuses_naming_file_and_hashes(tmp_path):
    f = tmp_path / "model.onnx"
    f.write_bytes(b"tampered weights")
    expected = hashlib.sha256(b"weights").hexdigest()
    actual = hashlib.sha256(b"tampered weights").hexdigest()
    with pytest.raises(SystemExit) as exc:
        fetch_face_models.verify_sha256(f, expected, label="model.onnx")
    message = str(exc.value)
    assert "model.onnx" in message
    assert expected in message and actual in message


def test_install_refuses_a_tampered_member_and_leaves_dest_empty(tmp_path):
    """The extract-verify-move order is the guarantee: a bad member must
    never appear in the destination, not even transiently alongside a good
    one — a partially-populated model dir would pass the engine's
    existence check and run one unverified model."""
    import zipfile

    good = b"detector-weights"
    bad = b"evil-embedder-weights"
    zip_path = tmp_path / "pack.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("buffalo_l/det_10g.onnx", good)
        z.writestr("buffalo_l/w600k_r50.onnx", bad)

    dest = tmp_path / "models"
    pins = {
        "det_10g.onnx": hashlib.sha256(good).hexdigest(),
        "w600k_r50.onnx": hashlib.sha256(b"the-real-embedder").hexdigest(),
    }
    with pytest.raises(SystemExit):
        fetch_face_models.install_from_zip(zip_path, dest, pins)
    assert not dest.exists() or not any(dest.iterdir())


def test_install_places_verified_members(tmp_path):
    import zipfile

    det = b"detector-weights"
    emb = b"embedder-weights"
    zip_path = tmp_path / "pack.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("buffalo_l/det_10g.onnx", det)
        z.writestr("buffalo_l/w600k_r50.onnx", emb)

    dest = tmp_path / "models"
    pins = {
        "det_10g.onnx": hashlib.sha256(det).hexdigest(),
        "w600k_r50.onnx": hashlib.sha256(emb).hexdigest(),
    }
    fetch_face_models.install_from_zip(zip_path, dest, pins)
    assert (dest / "det_10g.onnx").read_bytes() == det
    assert (dest / "w600k_r50.onnx").read_bytes() == emb


def test_the_committed_pins_are_real_hashes():
    """64 lowercase hex chars each, all distinct — a placeholder pin would
    make every verification a guaranteed refusal (or worse, a copy-paste
    duplicate would accept the wrong file)."""
    pins = dict(fetch_face_models.MODEL_SHA256)
    assert set(pins) == {"det_10g.onnx", "w600k_r50.onnx"}
    values = list(pins.values()) + [fetch_face_models.ZIP_SHA256]
    for h in values:
        assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)
    assert len(set(values)) == 3


def test_rerun_with_models_present_verifies_them(tmp_path, monkeypatch):
    """demo.sh promises 'fetch once, VERIFY ALWAYS'. The already-present
    branch of main() must actually re-hash the files — the F8 review's S3
    mutant deleted that loop and every test stayed green, leaving a
    tampered models/ dir verified-never after the first fetch. The download
    is stubbed to prove the failure comes from verification, not a fetch."""
    for name, _ in fetch_face_models.MODEL_SHA256.items():
        (tmp_path / name).write_bytes(b"tampered")
    monkeypatch.setattr(fetch_face_models.urllib.request, "urlretrieve",
                        lambda *a, **k: pytest.fail("must not re-download"))
    monkeypatch.setattr(fetch_face_models.sys, "argv",
                        ["fetch_face_models.py", str(tmp_path)])
    with pytest.raises(SystemExit, match="sha256 mismatch"):
        fetch_face_models.main()
