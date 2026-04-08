"""Utility helpers: structured logging, retry/backoff, env config."""

from __future__ import annotations

import json
import logging
import os
import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, TypeVar

T = TypeVar("T")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "lvl": record.levelname,
            "name": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for field in ("event", "component"):
            if hasattr(record, field):
                payload[field] = getattr(record, field)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger("newsmonitor")
    if logger.handlers:
        return logger
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    return logger


@dataclass
class RetryConfig:
    attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 8.0
    jitter: float = 0.2
    retry_exceptions: tuple[type[BaseException], ...] = (Exception,)


def retry_call(fn: Callable[[], T], cfg: RetryConfig, logger: logging.Logger, op_name: str) -> T:
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
            logger.warning(
                "%s failed; retrying",
                op_name,
                extra={"event": "retry", "component": "network"},
            )
            time.sleep(delay)
    assert err is not None
    raise err


def env_secret(name: str, fallback: str = "") -> str:
    return os.getenv(name, "").strip() or fallback
