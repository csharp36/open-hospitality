"""Face matching (F2): pure decision logic + the self-hosted ONNX engine.

Two halves, deliberately separable:

- The DECISION half (`identify`, `cosine_similarity`, the byte codecs) is
  pure Python over lists of floats — no numpy, no models — because that is
  where the compliance behavior lives and it must be testable everywhere
  the suite runs. `matched` requires clearing the threshold AND the
  top1-top2 ambiguity margin (plan decision 8); an empty enrollment is
  `no_template`, not a failed match; and templates from a different model
  version REFUSE to be compared — cross-model similarities are garbage
  that could verify the wrong person.

- The ENGINE half (`OnnxFaceEngine`) wraps the sha256-pinned buffalo_l
  models (SCRFD detector + ArcFace-class w600k_r50 embedder) via
  onnxruntime, CPU-only, no network at inference. Its dependencies are the
  `face` extra; everything is imported lazily so the core install never
  pays for them. Matching is tier 2 of docs/reference/biometric-attendance.md:
  our own code, wherever hosted — no vendor ever sees a face.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from math import sqrt
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # heavy deps: face extra only, imported lazily at runtime
    import numpy
    import numpy.typing

# The pack this version string names is pinned by sha256 in
# scripts/fetch_face_models.py; enrollment (F3) stamps it on every template
# row so a model upgrade cannot silently compare incomparable vectors.
MODEL_VERSION = "buffalo_l-w600k_r50"

_DETECTOR_FILE = "det_10g.onnx"
_EMBEDDER_FILE = "w600k_r50.onnx"


class FaceEmbedder(Protocol):
    """What the enrollment/kiosk APIs need from an engine — injected into
    `create_app` like every other seam, so tests run no models."""

    model_version: str

    def embed_largest_face(self, image_bytes: bytes) -> list[float] | None: ...


class ModelVersionMismatch(ValueError):
    """A stored template came from a different embedding model than the one
    loaded. Refused rather than scored: the fix is re-enrollment, not a
    lucky similarity."""


class FaceModelsMissing(RuntimeError):
    """The ONNX model files are not in the configured model dir. They are
    never committed — run scripts/fetch_face_models.py."""


@dataclass(frozen=True)
class Template:
    """One enrolled employee's embedding, as the matcher needs it."""

    employee_id: int
    embedding: list[float]
    model_version: str


@dataclass(frozen=True)
class MatchResult:
    # "matched" | "no_match" | "no_template"
    state: str
    employee_id: int | None
    # Best cosine similarity seen (None only for no_template). A no_match
    # still reports it: the punch row records how close the call was.
    score: float | None


def embedding_to_bytes(vec: list[float]) -> bytes:
    """float32 little-endian — the models emit float32, so nothing is lost;
    the result is what EncryptedBytes encrypts at rest."""
    return struct.pack(f"<{len(vec)}f", *vec)


def embedding_from_bytes(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = sqrt(sum(x * x for x in a))
    norm_b = sqrt(sum(y * y for y in b))
    return dot / (norm_a * norm_b)


def identify(
    probe: list[float],
    templates: list[Template],
    *,
    threshold: float,
    margin: float,
    model_version: str,
) -> MatchResult:
    """1:N identification with an ambiguity margin.

    `matched` requires the best score to clear `threshold` AND to beat the
    runner-up by `margin` — a close second-best is never a guess. With a
    single candidate there is no runner-up and the margin cannot fail.
    """
    for t in templates:
        if t.model_version != model_version:
            raise ModelVersionMismatch(
                f"template for employee {t.employee_id} was computed by "
                f"{t.model_version!r} but the loaded model is "
                f"{model_version!r}; re-enroll rather than compare"
            )
    if not templates:
        return MatchResult(state="no_template", employee_id=None, score=None)

    scored = sorted(
        ((cosine_similarity(probe, t.embedding), t.employee_id) for t in templates),
        reverse=True,
    )
    best_score, best_id = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else None
    if best_score >= threshold and (
        runner_up is None or best_score - runner_up >= margin
    ):
        return MatchResult(state="matched", employee_id=best_id, score=best_score)
    return MatchResult(state="no_match", employee_id=None, score=best_score)


# --- the ONNX engine ---------------------------------------------------------

# ArcFace's canonical five landmarks for a 112x112 crop (left eye, right
# eye, nose, left mouth corner, right mouth corner) — alignment warps the
# detected landmarks onto these before embedding.
_ARCFACE_DST = (
    (38.2946, 51.6963),
    (73.5318, 51.5014),
    (56.0252, 71.7366),
    (41.5493, 92.3655),
    (70.7299, 92.2041),
)

_DETECT_SIZE = 640
_DETECT_SCORE_THRESHOLD = 0.5
_NMS_IOU = 0.4


class OnnxFaceEngine:
    """Detect the largest face in an image and embed it. CPU-only, local.

    Construction fails loudly if the model files are absent (they are
    fetched, never committed) or if the `face` extra is not installed.
    """

    def __init__(self, model_dir: Path) -> None:
        try:
            import onnxruntime  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover - environment-dependent
            raise FaceModelsMissing(
                "onnxruntime is not installed; install the 'face' extra "
                "(uv sync --extra face) to run matching"
            ) from exc

        detector_path = model_dir / _DETECTOR_FILE
        embedder_path = model_dir / _EMBEDDER_FILE
        if not (detector_path.is_file() and embedder_path.is_file()):
            raise FaceModelsMissing(
                f"face models not found under {model_dir}; run "
                "scripts/fetch_face_models.py (they are downloaded "
                "sha256-pinned, never committed)"
            )
        opts = onnxruntime.SessionOptions()
        opts.log_severity_level = 3
        self._detector = onnxruntime.InferenceSession(
            str(detector_path), opts, providers=["CPUExecutionProvider"]
        )
        self._embedder = onnxruntime.InferenceSession(
            str(embedder_path), opts, providers=["CPUExecutionProvider"]
        )
        self.model_version = MODEL_VERSION

    def embed_largest_face(self, image_bytes: bytes) -> list[float] | None:
        """The embedding of the largest detected face, or None if the frame
        holds no face. Multiple bystanders in a kiosk frame are expected —
        largest box is the person at the camera."""
        import numpy as np

        image = self._decode_rgb(image_bytes)
        # Two passes: an extreme close-up (someone leaning into the kiosk
        # camera) puts the face above the detector's trained scale range,
        # and its score collapses. Zooming out recovers it — pay the second
        # inference only when the first finds nothing.
        faces = self._detect(image) or self._detect(image, fill=0.7)
        if not faces:
            return None
        box, landmarks = max(
            faces, key=lambda f: (f[0][2] - f[0][0]) * (f[0][3] - f[0][1])
        )
        aligned = self._align(image, landmarks)
        blob = ((aligned.astype(np.float32) - 127.5) / 127.5).transpose(2, 0, 1)
        (embedding,) = self._embedder.run(
            None, {self._embedder.get_inputs()[0].name: blob[np.newaxis]}
        )
        return [float(x) for x in embedding[0]]

    @staticmethod
    def _decode_rgb(image_bytes: bytes) -> numpy.typing.NDArray[numpy.uint8]:
        import io

        import numpy as np
        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as img:
            return np.asarray(img.convert("RGB"), dtype=np.uint8)

    def _detect(
        self, image: numpy.typing.NDArray[numpy.uint8], *, fill: float = 1.0
    ) -> list[
        tuple[numpy.typing.NDArray[numpy.float32], numpy.typing.NDArray[numpy.float32]]
    ]:
        """SCRFD decode: letterbox to 640x640 (at `fill` of the canvas),
        run, turn the per-stride distance predictions into boxes + five
        landmarks in ORIGINAL image coordinates, then NMS.
        Returns [(box[4], landmarks[5,2]), ...]."""
        import numpy as np

        height, width = image.shape[:2]
        scale = _DETECT_SIZE * fill / max(height, width)
        new_w, new_h = round(width * scale), round(height * scale)
        resized = self._resize(image, new_w, new_h)
        canvas = np.zeros((_DETECT_SIZE, _DETECT_SIZE, 3), dtype=np.uint8)
        canvas[:new_h, :new_w] = resized

        blob = ((canvas.astype(np.float32) - 127.5) / 128.0).transpose(2, 0, 1)
        outputs = self._detector.run(
            None, {self._detector.get_inputs()[0].name: blob[np.newaxis]}
        )

        boxes_all, kps_all, scores_all = [], [], []
        # det_10g emits 9 outputs: scores, bbox distances, and landmark
        # distances for strides 8/16/32, two anchors per grid cell.
        for idx, stride in enumerate((8, 16, 32)):
            scores = outputs[idx].reshape(-1)
            bbox_d = outputs[idx + 3].reshape(-1, 4) * stride
            kps_d = outputs[idx + 6].reshape(-1, 10) * stride
            side = _DETECT_SIZE // stride
            grid = np.stack(
                np.meshgrid(np.arange(side), np.arange(side)), axis=-1
            ).reshape(-1, 2).astype(np.float32) * stride
            centers = np.repeat(grid, 2, axis=0)  # two anchors per cell

            keep = scores >= _DETECT_SCORE_THRESHOLD
            if not keep.any():
                continue
            c, s = centers[keep], scores[keep]
            bd, kd = bbox_d[keep], kps_d[keep]
            boxes = np.stack(
                [c[:, 0] - bd[:, 0], c[:, 1] - bd[:, 1],
                 c[:, 0] + bd[:, 2], c[:, 1] + bd[:, 3]], axis=-1,
            )
            kps = kd.reshape(-1, 5, 2) + c[:, np.newaxis, :]
            boxes_all.append(boxes / scale)
            kps_all.append(kps / scale)
            scores_all.append(s)

        if not boxes_all:
            return []
        boxes = np.concatenate(boxes_all)
        kps = np.concatenate(kps_all)
        scores = np.concatenate(scores_all)
        keep_idx = self._nms(boxes, scores)
        return [
            (boxes[i].astype(np.float32), kps[i].astype(np.float32))
            for i in keep_idx
        ]

    @staticmethod
    def _nms(
        boxes: numpy.typing.NDArray[numpy.float32],
        scores: numpy.typing.NDArray[numpy.float32],
    ) -> list[int]:
        import numpy as np

        order = np.argsort(scores)[::-1]
        keep: list[int] = []
        areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
        while order.size:
            i = int(order[0])
            keep.append(i)
            rest = order[1:]
            xx1 = np.maximum(boxes[i, 0], boxes[rest, 0])
            yy1 = np.maximum(boxes[i, 1], boxes[rest, 1])
            xx2 = np.minimum(boxes[i, 2], boxes[rest, 2])
            yy2 = np.minimum(boxes[i, 3], boxes[rest, 3])
            inter = np.maximum(xx2 - xx1, 0) * np.maximum(yy2 - yy1, 0)
            iou = inter / (areas[i] + areas[rest] - inter)
            order = rest[iou <= _NMS_IOU]
        return keep

    @staticmethod
    def _resize(
        image: numpy.typing.NDArray[numpy.uint8], width: int, height: int
    ) -> numpy.typing.NDArray[numpy.uint8]:
        import numpy as np
        from PIL import Image

        return np.asarray(
            Image.fromarray(image).resize((width, height), Image.Resampling.BILINEAR),
            dtype=np.uint8,
        )

    @staticmethod
    def _align(
        image: numpy.typing.NDArray[numpy.uint8],
        landmarks: numpy.typing.NDArray[numpy.float32],
    ) -> numpy.typing.NDArray[numpy.uint8]:
        """Similarity transform (rotation+scale+translation, least squares)
        from the detected five landmarks onto ArcFace's canonical points,
        applied via PIL's inverse-mapping affine warp -> 112x112 crop."""
        import numpy as np
        from PIL import Image

        src = landmarks.astype(np.float64)
        dst = np.array(_ARCFACE_DST, dtype=np.float64)

        # Umeyama: M maps src -> dst.
        src_mean, dst_mean = src.mean(0), dst.mean(0)
        src_c, dst_c = src - src_mean, dst - dst_mean
        cov = dst_c.T @ src_c / len(src)
        u, s, vt = np.linalg.svd(cov)
        d = np.sign(np.linalg.det(u) * np.linalg.det(vt))
        diag = np.diag([1.0, d])
        rotation = u @ diag @ vt
        scale = np.trace(np.diag(s) @ diag) / (src_c**2).sum(axis=None) * len(src)
        translation = dst_mean - scale * rotation @ src_mean

        matrix = np.eye(3)
        matrix[:2, :2] = scale * rotation
        matrix[:2, 2] = translation
        inverse = np.linalg.inv(matrix)  # PIL wants the dst -> src mapping
        warped = Image.fromarray(image).transform(
            (112, 112),
            Image.Transform.AFFINE,
            data=tuple(inverse[:2].reshape(-1)),
            resample=Image.Resampling.BILINEAR,
        )
        return np.asarray(warped, dtype=np.uint8)
