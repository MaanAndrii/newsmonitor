"""Utility helpers: retry/backoff and env config."""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

T = TypeVar("T")


@dataclass
class RetryConfig:
    attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 8.0
    jitter: float = 0.2
    retry_exceptions: tuple[type[BaseException], ...] = (Exception,)


def retry_call(fn: Callable[[], T], cfg: RetryConfig, op_name: str = "operation") -> T:
    err: BaseException | None = None
    for i in range(1, cfg.attempts + 1):
        try:
            return fn()
        except cfg.retry_exceptions as exc:  # type: ignore[misc]
            err = exc
            if i >= cfg.attempts:
                break
            delay = min(cfg.base_delay * (2 ** (i - 1)), cfg.max_delay)
            delay += random.uniform(0, cfg.jitter)
            print(f"[retry] {op_name}: attempt {i}/{cfg.attempts} failed ({exc}); sleep {delay:.2f}s")
            time.sleep(delay)
    assert err is not None
    raise err


def env_secret(name: str, fallback: str = "") -> str:
    return os.getenv(name, "").strip() or fallback
