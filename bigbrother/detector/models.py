"""Object detection models behind a hardware-agnostic interface.

A model = an ONNX file + a decoder for its output format. The backend
(OpenVINO on CPU today, ONNX Runtime with CUDA/TensorRT once a GPU is added)
is chosen in config; nothing else in the system knows which one is running.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np

log = logging.getLogger(__name__)

COCO_LABELS = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
    "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog",
    "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket", "bottle",
    "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch", "potted plant",
    "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard", "cell phone",
    "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase", "scissors",
    "teddy bear", "hair drier", "toothbrush",
]


@dataclass(frozen=True)
class Detection:
    label: str
    score: float
    box: tuple[float, float, float, float]  # x1, y1, x2, y2 in source-image pixels
    votes: tuple[tuple[str, float], ...] = ()  # other labels the model gave this same object

    @property
    def area(self) -> float:
        return max(0.0, self.box[2] - self.box[0]) * max(0.0, self.box[3] - self.box[1])


class Backend(Protocol):
    input_shape: tuple[int, int]  # (height, width)

    def infer(self, tensor: np.ndarray) -> np.ndarray: ...


def disable_openvino_telemetry() -> None:
    """Opt out of OpenVINO's usage telemetry before it is imported.

    `import openvino` sends a usage event to Google Analytics unless a consent
    file says "0" (the same file Intel's `opt_in_out --opt_out` writes). A
    camera system has no business phoning home, so we write it ourselves. The
    Docker image also opts out at build time.
    """
    consent = Path.home() / "intel" / "openvino_telemetry"
    try:
        if not consent.exists() or consent.read_text().strip() != "0":
            consent.parent.mkdir(parents=True, exist_ok=True)
            consent.write_text("0")
    except OSError as exc:
        log.warning("could not write OpenVINO telemetry opt-out (%s)", exc)


class OpenVINOBackend:
    """Intel CPUs (and Intel GPUs). Default for the R730xd today."""

    def __init__(self, model_path: Path, device: str = "CPU", threads: int = 0,
                 input_hw: tuple[int, int] | None = None):
        disable_openvino_telemetry()
        import openvino as ov

        core = ov.Core()
        model = core.read_model(str(model_path))
        if input_hw:
            # YOLOX is fully convolutional, so the input can be any multiple of 32.
            model.reshape({model.input(0).any_name: [1, 3, *input_hw]})
        props = {"PERFORMANCE_HINT": "LATENCY"}
        if threads:
            props["INFERENCE_NUM_THREADS"] = threads
        self._compiled = core.compile_model(model, device, props)
        shape = self._compiled.input(0).shape
        self.input_shape = (int(shape[2]), int(shape[3]))
        self._local = threading.local()  # one infer request per calling thread

    def infer(self, tensor: np.ndarray) -> np.ndarray:
        req = getattr(self._local, "req", None)
        if req is None:
            req = self._local.req = self._compiled.create_infer_request()
        return req.infer({0: tensor})[0]


class OnnxRuntimeBackend:
    """Portable fallback, and the NVIDIA path (CUDA / TensorRT execution providers)."""

    PROVIDERS = {
        "cpu": ["CPUExecutionProvider"],
        "cuda": ["CUDAExecutionProvider", "CPUExecutionProvider"],
        "tensorrt": ["TensorrtExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"],
    }

    def __init__(self, model_path: Path, device: str = "cpu", threads: int = 0,
                 input_hw: tuple[int, int] | None = None):
        import onnxruntime as ort

        if input_hw:
            log.warning("onnxruntime runs the model at its own input size; input_size is ignored")

        opts = ort.SessionOptions()
        if threads:
            opts.intra_op_num_threads = threads
        self._sess = ort.InferenceSession(str(model_path), opts, providers=self.PROVIDERS[device.lower()])
        inp = self._sess.get_inputs()[0]
        self._name = inp.name
        self.input_shape = (int(inp.shape[2]), int(inp.shape[3]))

    def infer(self, tensor: np.ndarray) -> np.ndarray:
        return self._sess.run(None, {self._name: tensor})[0]


def make_backend(kind: str, model_path: Path, device: str, threads: int,
                 input_hw: tuple[int, int] | None = None) -> Backend:
    if kind == "openvino":
        return OpenVINOBackend(model_path, device or "CPU", threads, input_hw)
    if kind == "onnxruntime":
        return OnnxRuntimeBackend(model_path, device or "cpu", threads, input_hw)
    raise ValueError(f"unknown detector backend '{kind}' (use openvino or onnxruntime)")


def nms(boxes: np.ndarray, scores: np.ndarray, iou: float) -> np.ndarray:
    """Plain greedy NMS. boxes: (N,4) xyxy. Returns kept indices, best first."""
    order = scores.argsort()[::-1]
    x1, y1, x2, y2 = boxes.T
    areas = (x2 - x1) * (y2 - y1)
    keep = []
    while order.size:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.clip(xx2 - xx1, 0, None) * np.clip(yy2 - yy1, 0, None)
        ovr = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
        order = order[1:][ovr <= iou]
    return np.array(keep, dtype=int)


class YoloxModel:
    """YOLOX (Apache-2.0). Input: BGR 0-255, letterboxed top-left with 114 padding."""

    STRIDES = (8, 16, 32)

    def __init__(self, backend: Backend, labels: list[str] = COCO_LABELS):
        self.backend = backend
        self.labels = labels
        h, w = backend.input_shape
        grids, strides = [], []
        for s in self.STRIDES:
            hs, ws = h // s, w // s
            xv, yv = np.meshgrid(np.arange(ws), np.arange(hs))
            grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
            strides.append(np.full((hs * ws, 1), s))
        self._grid = np.concatenate(grids, 0).astype(np.float32)
        self._stride = np.concatenate(strides, 0).astype(np.float32)

    def preprocess(self, img: np.ndarray) -> tuple[np.ndarray, float]:
        h, w = self.backend.input_shape
        r = min(h / img.shape[0], w / img.shape[1])
        resized = cv2.resize(img, (int(img.shape[1] * r), int(img.shape[0] * r)),
                             interpolation=cv2.INTER_LINEAR)
        padded = np.full((h, w, 3), 114, dtype=np.uint8)
        padded[: resized.shape[0], : resized.shape[1]] = resized
        tensor = padded.transpose(2, 0, 1)[None].astype(np.float32)
        return np.ascontiguousarray(tensor), r

    def decode(self, out: np.ndarray, ratio: float, min_score: float, iou: float = 0.45,
               keep_labels: set[str] | None = None) -> list[Detection]:
        pred = out[0]
        xy = (pred[:, :2] + self._grid) * self._stride
        wh = np.exp(pred[:, 2:4]) * self._stride
        cls_scores = pred[:, 5:] * pred[:, 4:5]
        cls = cls_scores.argmax(1)
        score = cls_scores[np.arange(len(cls)), cls]
        mask = score >= min_score
        if keep_labels is not None:
            wanted = [i for i, name in enumerate(self.labels) if name in keep_labels]
            mask &= np.isin(cls, wanted)
        if not mask.any():
            return []
        xy, wh, cls, score = xy[mask], wh[mask], cls[mask], score[mask]
        boxes = np.concatenate([xy - wh / 2, xy + wh / 2], 1) / ratio
        # Class-aware NMS: offset boxes per class so different classes never suppress each other.
        offset = cls[:, None].astype(np.float32) * 10000.0
        keep = nms(boxes + offset, score, iou)
        return [Detection(self.labels[cls[i]], float(score[i]), tuple(float(v) for v in boxes[i]))
                for i in keep]

    def detect(self, img: np.ndarray, min_score: float = 0.4,
               keep_labels: set[str] | None = None) -> list[Detection]:
        tensor, ratio = self.preprocess(img)
        out = self.backend.infer(tensor)
        dets = self.decode(out, ratio, min_score, keep_labels=keep_labels)
        h, w = img.shape[:2]
        return [Detection(d.label, d.score, (max(0.0, d.box[0]), max(0.0, d.box[1]),
                                             min(float(w), d.box[2]), min(float(h), d.box[3])))
                for d in dets]


MODEL_FAMILIES = {"yolox": YoloxModel}


def load_model(family: str, path: Path, backend: str, device: str = "", threads: int = 0,
               input_size: tuple[int, int] | None = None):
    """input_size is (width, height); None keeps the model's own input size."""
    if family not in MODEL_FAMILIES:
        raise ValueError(f"unknown model family '{family}' (available: {', '.join(MODEL_FAMILIES)})")
    input_hw = (input_size[1], input_size[0]) if input_size else None
    m = MODEL_FAMILIES[family](make_backend(backend, path, device, threads, input_hw))
    h, w = m.backend.input_shape
    log.info("loaded %s model %s on %s/%s, input %dx%d", family, path.name, backend, device or "default", w, h)
    return m


def cpu_count() -> int:
    """CPUs this process may use (respects container CPU limits/affinity)."""
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:  # not Linux
        return os.cpu_count() or 1


def plan_threads(workers: int, threads: int, cpus: int | None = None) -> tuple[int, int]:
    """Split the CPUs between parallel inferences so they don't fight over cores.

    Several cameras each running the model with every core at once is what
    made inference ~10x slower on a busy laptop. Default: one inference at a
    time per 8 CPUs, each using its share of the cores.
    """
    cpus = cpus or cpu_count()
    workers = workers or max(1, cpus // 8)
    # Leave cores free on bigger machines: OpenVINO's threads spin while they work, and if the
    # model takes every core, go2rtc and the app relaying live video stutter.
    usable = cpus - (2 if cpus >= 12 else 1 if cpus >= 6 else 0)
    threads = threads or max(1, usable // workers)
    return workers, threads


def lower_priority(step: int = 10) -> None:
    """Run inference behind recording and live video when the CPU is contended (Unix only)."""
    try:
        os.nice(step)
    except (AttributeError, OSError):
        pass


class InferencePool:
    """All cameras share one model; requests queue for a fixed number of workers."""

    def __init__(self, model, workers: int):
        self.model = model
        self.workers = workers
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="infer")

    def detect(self, img: np.ndarray, min_score: float, labels: set[str] | None = None) -> list[Detection]:
        return self.detect_timed(img, min_score, labels)[0]

    def detect_timed(self, img: np.ndarray, min_score: float,
                     labels: set[str] | None = None) -> tuple[list[Detection], float, float]:
        """Detections plus (inference ms, ms spent queued behind other cameras)."""
        submitted = time.perf_counter()
        started = []

        def job():
            started.append(time.perf_counter())
            return self.model.detect(img, min_score, labels)

        dets = self._pool.submit(job).result()
        done = time.perf_counter()
        return dets, (done - started[0]) * 1000, (started[0] - submitted) * 1000

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
