"""Utility helpers: structured logging, retry/backoff and env config."""

from __future__ import annotations

import json
import logging
import os
import random
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

T = TypeVar("T")


class JsonFormatter(logging.Formatter):
    """Small JSON formatter for predictable machine-readable logs."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "name": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(name: str = "newsmonitor", level: str | None = None) -> logging.Logger:
    """Create/reuse logger with JSON formatter."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    level_name = (level or os.getenv("NEWSMONITOR_LOG_LEVEL", "INFO")).upper()
    logger.setLevel(getattr(logging, level_name, logging.INFO))
    logger.propagate = False

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    return logger


@dataclass
class RetryConfig:
    attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 8.0
    jitter: float = 0.2
    retry_exceptions: tuple[type[BaseException], ...] = (Exception,)


def retry_call(
    fn: Callable[[], T],
    cfg: RetryConfig,
    op_name: str = "operation",
    logger: logging.Logger | None = None,
) -> T:
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
            msg = f"[retry] {op_name}: attempt {i}/{cfg.attempts} failed ({exc}); sleep {delay:.2f}s"
            if logger:
                logger.warning(msg)
            else:
                print(msg)
            time.sleep(delay)
    assert err is not None
    raise err


def env_secret(name: str, fallback: str = "") -> str:
    return os.getenv(name, "").strip() or fallback
