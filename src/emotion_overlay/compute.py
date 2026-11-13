"""CPU or GPU: what's available, and which device a model should use.

"cpu", "cuda" and "auto" are the per-model `device` settings. Asking for the GPU when
it isn't usable falls back to the CPU with a warning instead of failing, so a config
copied to another PC still starts.
"""

import logging
from dataclasses import asdict, dataclass
from typing import Any

log = logging.getLogger(__name__)

INSTALL_HINT = (
    "install GPU support (run-gpu.bat or `uv sync --extra gpu` for NVIDIA; run-amd.bat or "
    "`uv sync --extra amd` on Linux for AMD), then restart the app"
)


@dataclass(frozen=True)
class GpuInfo:
    available: bool
    name: str | None
    reason: str | None  # why it can't be used, for the setup page

    def to_api(self) -> dict[str, Any]:
        return asdict(self)


def gpu_vendor() -> str | None:
    """Which GPU this PyTorch build is for: "nvidia", "amd", or None (CPU-only). AMD's
    ROCm builds answer through torch.cuda too, so the device is still called "cuda"."""
    import torch

    if getattr(torch.version, "hip", None):
        return "amd"
    return "nvidia" if torch.version.cuda else None


def gpu_info() -> GpuInfo:
    import torch

    vendor = gpu_vendor()
    if vendor is None:
        return GpuInfo(False, None, f"this copy of PyTorch is CPU-only: {INSTALL_HINT}")
    if not torch.cuda.is_available():
        what = "AMD GPU supported by ROCm" if vendor == "amd" else "NVIDIA GPU"
        return GpuInfo(False, None, f"no {what} (or its driver) was found")
    return GpuInfo(True, torch.cuda.get_device_name(0), None)


def torch_device(requested: str, what: str) -> str:
    """'cuda' or 'cpu' for a PyTorch model, given its `device` setting."""
    if requested == "cpu":
        return "cpu"
    info = gpu_info()
    if info.available:
        return "cuda"
    if requested == "cuda":
        log.warning("%s: GPU requested but %s; using the CPU", what, info.reason)
    return "cpu"


def release_gpu_memory() -> None:
    """Give memory held by unloaded models back to the driver (after switching)."""
    import gc

    import torch

    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
