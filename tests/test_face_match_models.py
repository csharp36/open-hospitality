"""F2: the ONNX engine against the real (fetched, never committed) models.

These tests SKIP when the `face` extra or the fetched models are absent —
the suite must stay runnable on a bare checkout. When they do run, they
close the loop the pure-logic tests cannot: the detector finds the
synthetic fixtures' faces, the embedder puts two captures of the same
(nonexistent) person above the shipped threshold, and two different people
land far below it with room for the margin.
"""

from pathlib import Path

import pytest

from usali.config import get_settings
from usali.face_match import (
    MODEL_VERSION,
    Template,
    cosine_similarity,
    embedding_from_bytes,
    embedding_to_bytes,
    identify,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "faces"


def _engine():
    # Skip ONLY on the two absences (models not fetched, `face` extra not
    # installed) — a genuine bug in OnnxFaceEngine.__init__ must FAIL these
    # tests, not silently skip them as "models unavailable" (F8).
    try:
        from usali.face_match import FaceModelsMissing, OnnxFaceEngine
    except ImportError as exc:
        pytest.skip(f"face extra not installed: {exc}")
    try:
        return OnnxFaceEngine(get_settings().face_model_dir)
    except (FaceModelsMissing, ImportError) as exc:
        pytest.skip(f"face models unavailable: {exc}")


@pytest.fixture(scope="module")
def engine():
    return _engine()


@pytest.fixture(scope="module")
def embeddings(engine):
    out = {}
    for name in ("person_a", "person_a_dim", "person_b"):
        vec = engine.embed_largest_face((_FIXTURES / f"{name}.jpg").read_bytes())
        assert vec is not None, f"detector found no face in {name}.jpg"
        out[name] = vec
    return out


def test_a_faceless_image_embeds_to_none(engine):
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (640, 480), (128, 100, 90)).save(buf, format="JPEG")
    assert engine.embed_largest_face(buf.getvalue()) is None


def test_same_person_across_capture_conditions_clears_the_threshold(embeddings):
    """person_a_dim is a darkened, re-cropped capture — the enrollment
    photo and the kiosk frame will never match conditions, so the shipped
    default threshold must hold across that gap."""
    settings = get_settings()
    score = cosine_similarity(embeddings["person_a"], embeddings["person_a_dim"])
    assert score >= settings.face_match_threshold


def test_different_people_score_far_below_the_threshold(embeddings):
    settings = get_settings()
    score = cosine_similarity(embeddings["person_a"], embeddings["person_b"])
    assert score < settings.face_match_threshold - settings.face_match_margin


def test_identify_picks_the_right_person_end_to_end(embeddings):
    """The full path a kiosk punch takes: enrolled templates from
    enrollment-quality frames, probe from a degraded frame, DB byte
    round-trip included."""
    settings = get_settings()
    templates = [
        Template(1, embedding_from_bytes(
            embedding_to_bytes(embeddings["person_a"])), MODEL_VERSION),
        Template(2, embedding_from_bytes(
            embedding_to_bytes(embeddings["person_b"])), MODEL_VERSION),
    ]
    result = identify(
        embeddings["person_a_dim"], templates,
        threshold=settings.face_match_threshold,
        margin=settings.face_match_margin,
        model_version=MODEL_VERSION,
    )
    assert result.state == "matched"
    assert result.employee_id == 1
