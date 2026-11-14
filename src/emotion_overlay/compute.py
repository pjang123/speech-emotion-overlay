"""CPU or GPU: what's available, and which device a model should use.

"cpu", "cuda" and "auto" are the per-model `device` settings; "cuda" means "the GPU",
whichever kind it is. Asking for the GPU when it isn't usable falls back to the CPU
with a warning instead of failing, so a config copied to another PC still starts.

GPUs: NVIDIA (CUDA builds of PyTorch), AMD (ROCm builds, which also answer through
torch.cuda) and Apple Silicon (Metal, PyTorch's "mps" device, in every macOS build).
Models report the PyTorch device they actually run on: "cuda", "mps" or "cpu".
"""

import functools
import logging
import subprocess
import sys
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
    """Which GPU this PyTorch build is for: "nvidia", "amd", "apple", or None (CPU-only).
    AMD's ROCm builds answer through torch.cuda too, so their device is still "cuda"."""
    import torch

    if getattr(torch.version, "hip", None):
        return "amd"
    if torch.version.cuda:
        return "nvidia"
    if sys.platform == "darwin" and torch.backends.mps.is_built():
        return "apple"
    return None


def is_gpu(device: str) -> bool:
    """True for a PyTorch GPU device name ("cuda" or "mps")."""
    return device in ("cuda", "mps")


@functools.lru_cache(maxsize=1)
def _apple_chip() -> str:
    """'Apple M2 Pro' etc., for the setup page."""
    try:
        out = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
        return out.stdout.strip() or "Apple Silicon"
    except (OSError, subprocess.SubprocessError):
        return "Apple Silicon"


def gpu_info() -> GpuInfo:
    import torch

    vendor = gpu_vendor()
    if vendor is None:
        return GpuInfo(False, None, f"this copy of PyTorch is CPU-only: {INSTALL_HINT}")
    if vendor == "apple":
        if torch.backends.mps.is_available():
            return GpuInfo(True, f"{_apple_chip()} GPU (Metal)", None)
        return GpuInfo(
            False,
            None,
            "PyTorch can't use this Mac's GPU (it needs Apple Silicon, macOS 14 or newer, "
            "and Metal, which virtual machines usually don't have)",
        )
    if not torch.cuda.is_available():
        what = "AMD GPU supported by ROCm" if vendor == "amd" else "NVIDIA GPU"
        return GpuInfo(False, None, f"no {what} (or its driver) was found")
    return GpuInfo(True, torch.cuda.get_device_name(0), None)


def torch_device(requested: str, what: str) -> str:
    """'cuda', 'mps' or 'cpu' for a PyTorch model, given its `device` setting."""
    if requested == "cpu":
        return "cpu"
    info = gpu_info()
    if info.available:
        return "mps" if gpu_vendor() == "apple" else "cuda"
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
    elif sys.platform == "darwin" and torch.backends.mps.is_available():
        torch.mps.empty_cache()
