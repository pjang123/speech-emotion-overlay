"""Cache-first model loading.

Hugging Face loaders contact the Hub on every start (update checks, and transformers
may even fetch a converted weights file from a pull-request branch). To keep the
"no network after first run" promise, always try the local cache first and only
go online when the model has never been downloaded.
"""

import logging
import os
from collections.abc import Callable
from typing import Any, TypeVar

log = logging.getLogger(__name__)

# For repos that only ship .bin weights, transformers starts a background thread
# that queries the Hub for a safetensors conversion on every load, even with
# local_files_only=True. Read at call time, so setting it on import is enough.
os.environ.setdefault("DISABLE_SAFETENSORS_CONVERSION", "1")

T = TypeVar("T")


def load_cached(loader: Callable[..., T], name: str, **kwargs: Any) -> T:  # noqa: UP047 - py3.11
    try:
        return loader(name, local_files_only=True, **kwargs)
    except (OSError, ValueError):
        log.info("'%s' is not downloaded yet; downloading once (progress below)...", name)
        return loader(name, **kwargs)
