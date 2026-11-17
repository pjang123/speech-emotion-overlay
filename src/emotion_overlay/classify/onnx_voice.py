"""The Vox-Profile voice model on the CPU, as a compressed copy run by ONNX Runtime.

On the CPU the full model is the app's heaviest part (1.2 GB of memory, ~0.3 s per
3 s of speech on 8 cores). The copy stores its matrix weights as 8-bit integers
(int8, "dynamic quantisation"): about 340 MB, ~2.6x faster, nearly the same scores.

There is nothing to download: the first time it's needed, the app converts the model
it already has (~1.5 minutes, once) and keeps the result in `paths.cache_dir()`, named
after the checkpoint's revision and the conversion recipe, so a new model version or
a changed recipe is converted again and the old copy deleted. If converting or loading
fails, the caller falls back to the full PyTorch model.
"""

import logging
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any

import numpy as np

from emotion_overlay.classify import vox_profile
from emotion_overlay.paths import cache_dir

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
RECIPE = 1  # bump when the conversion changes, so existing copies are made again
_convert_lock = threading.Lock()  # one conversion at a time (they need ~4 GB of memory)


def folder() -> Path:
    return cache_dir() / "voice"


def _prefix(model: str) -> str:
    return model.replace("/", "--") + "-"


def cache_file(model: str, weights: Path) -> Path:
    """Where the converted copy of `model` lives. The Hugging Face cache keeps each
    revision of a model in a folder named after its commit, so that identifies it."""
    revision = weights.parent.name[:12]
    return folder() / f"{_prefix(model)}{revision}-r{RECIPE}-int8.onnx"


def needs_converting(model: str) -> bool:
    """True if `model` is downloaded but its converted copy hasn't been made yet.
    Only looks at local files: never downloads anything."""
    if not vox_profile.is_vox_profile(model):
        return False
    weights = vox_profile.cached_weights_path(model)
    return weights is not None and not cache_file(model, weights).exists()


def prepare(model: str) -> Path:
    """The converted copy of `model`, converting it first if needed."""
    target = cache_file(model, vox_profile.weights_path(model))
    if target.exists():
        return target
    with _convert_lock:
        if not target.exists():  # someone else may have converted it meanwhile
            target.parent.mkdir(parents=True, exist_ok=True)
            log.info("Compressing the voice model for the CPU (first time only, ~1.5 min)...")
            convert(vox_profile.build(model), target)
            for old in target.parent.glob(f"{_prefix(model)}*.onnx"):
                if old != target:
                    old.unlink(missing_ok=True)  # an older revision or recipe
            log.info("Compressed voice model saved: %s", target)
    return target


def convert(module: Any, target: Path) -> None:
    """Export a torch voice model (waveform -> logits) to ONNX, then store its matrix
    multiplications' weights as int8. Written to a temporary name first, so an
    interrupted conversion never leaves a broken file behind."""
    import onnx
    import torch
    from onnxruntime.quantization import QuantType, quantize_dynamic

    # Both report every graph clean-up step as a warning; none needs attention here.
    for noisy in ("torch.onnx", "onnxscript"):
        logging.getLogger(noisy).setLevel(logging.ERROR)

    work = Path(tempfile.mkdtemp(prefix="convert-", dir=target.parent))
    try:
        full = work / "full.onnx"
        noise = np.random.default_rng(0).normal(0, 0.1, 2 * SAMPLE_RATE).astype(np.float32)
        signal = torch.from_numpy(vox_profile.normalise(noise))[None]
        # Any length of audio: 0.1 s up to a minute (longer would also work).
        samples = torch.export.Dim("samples", min=SAMPLE_RATE // 10, max=60 * SAMPLE_RATE)
        torch.onnx.export(
            module,
            (signal,),
            str(full),
            input_names=["signal"],
            output_names=["logits"],
            dynamic_shapes=({1: samples},),
            dynamo=True,
            external_data=False,  # one file (WavLM-large is 1.2 GB, under the 2 GB limit)
            verbose=False,
        )
        # The exporter records intermediate shapes that ONNX's own shape inference then
        # disagrees with, which stops the quantiser; they're optional, so drop them.
        graph = onnx.load(str(full))
        del graph.graph.value_info[:]
        onnx.save(graph, str(full))
        del graph
        compressed = work / "int8.onnx"
        quantize_dynamic(
            full, compressed, weight_type=QuantType.QInt8, op_types_to_quantize=["MatMul"]
        )
        compressed.replace(target)
    finally:
        shutil.rmtree(work, ignore_errors=True)


class OnnxVoiceModel:
    """Runs the converted copy: normalised waveform in, the model's logits out."""

    def __init__(self, path: Path) -> None:
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.log_severity_level = 3  # errors only
        self._session = ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])

    def logits(self, audio: np.ndarray) -> np.ndarray:
        signal = vox_profile.normalise(audio)[None]
        return self._session.run(["logits"], {"signal": signal})[0][0]
