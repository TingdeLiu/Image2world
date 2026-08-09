"""One process-wide lock for local GPU workloads.

The HTTP inference handlers and the long-running reconstruction worker share a
single 16 GB GPU.  A threading lock works across asyncio worker threads and the
background reconstruction thread, unlike an asyncio-only semaphore.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TypeVar


T = TypeVar("T")

GPU_JOB_LOCK = threading.Lock()


def run_with_gpu_lock(fn: Callable[..., T], *args, **kwargs) -> T:
    with GPU_JOB_LOCK:
        return fn(*args, **kwargs)

