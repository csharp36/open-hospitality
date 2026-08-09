"""F2: the matching DECISION logic, pure and model-free.

The ONNX engine (detector + embedder) is exercised by
tests/test_face_match_models.py, which skips when the fetched models are
absent — these tests pin the part that decides what a score MEANS, because
that is where the money/compliance behavior lives: `verified` requires
clearing the threshold AND the top1-top2 ambiguity margin (plan decision
8), an empty enrollment is `no_template` (not a failed match), and
embeddings from different model versions must refuse to be compared
rather than score garbage.
"""

import math

import pytest

from usali.face_match import (
    ModelVersionMismatch,
    Template,
    cosine_similarity,
    embedding_from_bytes,
    embedding_to_bytes,
    identify,
)

_V = "buffalo_l-w600k_r50"


def _unit(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec]


# Three well-separated directions and a near-neighbor of the first.
_A = _unit([1.0, 0.1, 0.0, 0.0])
_A_CLOSE = _unit([1.0, 0.2, 0.05, 0.0])   # cos(A, A_CLOSE) ~ 0.995
_B = _unit([0.0, 1.0, 0.1, 0.0])          # cos(A, B) ~ 0.11
_C = _unit([0.0, 0.0, 0.1, 1.0])


def test_embedding_bytes_roundtrip():
    """Templates live in the DB as encrypted bytes; the vector must survive
    the trip exactly (float32 — the models emit float32, so packing at that
    width loses nothing)."""
    vec = [0.25, -1.5, 3.0, 0.0625]
    assert embedding_from_bytes(embedding_to_bytes(vec)) == vec


def test_cosine_similarity_on_known_vectors():
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_a_clear_winner_matches():
    result = identify(
        _A, [Template(1, _A_CLOSE, _V), Template(2, _B, _V), Template(3, _C, _V)],
        threshold=0.60, margin=0.10, model_version=_V,
    )
    assert result.state == "matched"
    assert result.employee_id == 1
    assert result.score == pytest.approx(cosine_similarity(_A, _A_CLOSE))


def test_below_threshold_is_no_match_even_unambiguously():
    """B is the clear best candidate for probe C's neighborhood — but a
    best score under the threshold is a stranger, not a weak match."""
    result = identify(
        _C, [Template(1, _A, _V), Template(2, _B, _V)],
        threshold=0.60, margin=0.10, model_version=_V,
    )
    assert result.state == "no_match"
    assert result.employee_id is None
    assert result.score is not None and result.score < 0.60


def test_an_ambiguous_top_two_is_no_match_never_a_guess():
    """Two enrolled siblings both clear the threshold within the margin:
    the plan's decision 8 — a close second-best is grey, never a guess.
    The score reported is the best one, so the punch row still records
    how close the call was."""
    result = identify(
        _A, [Template(1, _A_CLOSE, _V), Template(2, _A, _V)],
        threshold=0.60, margin=0.10, model_version=_V,
    )
    assert result.state == "no_match"
    assert result.employee_id is None


def test_a_single_enrolled_template_can_still_match():
    """margin is top1-top2; with one candidate there is no top2 and the
    margin cannot fail the match."""
    result = identify(
        _A, [Template(7, _A_CLOSE, _V)],
        threshold=0.60, margin=0.10, model_version=_V,
    )
    assert result.state == "matched"
    assert result.employee_id == 7


def test_no_enrolled_templates_is_its_own_answer():
    """Distinct from no_match: matching ran and found NOBODY enrolled.
    The punch records no_template (grey), and the kiosk goes straight to
    search — the cold-start posture (plan decision 9)."""
    result = identify(_A, [], threshold=0.60, margin=0.10, model_version=_V)
    assert result.state == "no_template"
    assert result.employee_id is None
    assert result.score is None


def test_a_template_from_another_model_version_is_refused():
    """Embeddings from different models are not comparable — scoring them
    would produce garbage similarities that could VERIFY the wrong person.
    Loud refusal forces the re-enrollment migration instead."""
    with pytest.raises(ModelVersionMismatch, match="stale-model-v0"):
        identify(
            _A,
            [Template(1, _A_CLOSE, _V), Template(2, _B, "stale-model-v0")],
            threshold=0.60, margin=0.10, model_version=_V,
        )


def test_the_margin_is_checked_against_the_runner_up_not_the_threshold():
    """top1=0.99, top2=0.11: hugely unambiguous. A margin implementation
    that compared top1 against threshold+margin would also pass here, so
    pin the case that separates them: top1 barely over threshold with a
    distant top2 must still MATCH."""
    barely = _unit([1.0, 0.95, 0.0, 0.0])  # cos(A, barely) ~ 0.79
    result = identify(
        _A, [Template(1, barely, _V), Template(2, _C, _V)],
        threshold=0.75, margin=0.10, model_version=_V,
    )
    assert result.state == "matched"
    assert result.employee_id == 1


# Exact basis vectors: cosine(EX, EX) is float-exact 1.0 and
# cosine(EX, EY) is float-exact 0.0, so the boundary pins below have no
# rounding to hide behind (the module's _A/_B are approximate).
_EX = [1.0, 0.0, 0.0, 0.0]
_EY = [0.0, 1.0, 0.0, 0.0]


def test_a_score_exactly_at_threshold_matches():
    """The contract is `score >= threshold` — the boundary belongs to the
    match. An off-by-epsilon mutant (`>`) survived the F8 review because
    nothing sat exactly ON the line."""
    result = identify(
        _EX, [Template(1, _EX, _V)],
        threshold=1.0, margin=0.10, model_version=_V,
    )
    assert result.state == "matched"


def test_a_gap_exactly_at_the_margin_matches():
    """Same boundary pin for the ambiguity margin: top1=1.0 (identical),
    top2=0.0 (orthogonal), margin=1.0 — the gap exactly meets the margin
    and must match; `>` would call it ambiguous."""
    result = identify(
        _EX, [Template(1, _EX, _V), Template(2, _EY, _V)],
        threshold=0.5, margin=1.0, model_version=_V,
    )
    assert result.state == "matched"
    assert result.employee_id == 1
