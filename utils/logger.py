"""
Structured, async-safe logging setup.
Call setup_logging() once at startup in main.py.
"""

from __future__ import annotations
import logging
import sys
import time


class _NsFormatter(logging.Formatter):
    """Adds monotonic nanosecond timestamp to every log record."""

    def format(self, record: logging.LogRecord) -> str:
        record.mono_ns = time.monotonic_ns()
        return super().format(record)


def setup_logging(level: str = "INFO") -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        _NsFormatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s | mono_ns=%(mono_ns)d | %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.setLevel(numeric)
    root.handlers.clear()
    root.addHandler(handler)
