"""
log_setup.py — Shared logging factory for all engine components.

All engine-related modules (engine, executor, blocking_ib_client) use
get_engine_logger() so format, path, and daily-rotation are consistent.

Log files are named  logs/engine_YYYY-MM-DD.log  (one file per calendar day).
scan_premiums uses get_scanner_logger() for the same daily-rotation pattern.
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path


def _today() -> str:
    return date.today().strftime("%Y-%m-%d")


def _make_handler(log_path: Path) -> logging.FileHandler:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S ET",
    ))
    return handler


def _suppress_noise() -> None:
    """Silence chatty third-party loggers."""
    logging.getLogger("ib_async").setLevel(logging.WARNING)
    logging.getLogger("ib_async.wrapper").setLevel(logging.ERROR)
    logging.getLogger("ib_async.client").setLevel(logging.WARNING)


def get_engine_logger(name: str, logs_dir: Path) -> logging.Logger:
    """
    Return a logger that writes to logs/engine_YYYY-MM-DD.log.
    Safe to call multiple times with the same name (idempotent).
    """
    _suppress_noise()
    logger = logging.getLogger(name)
    if not logger.handlers:
        log_file = logs_dir / f"engine_{_today()}.log"
        logger.addHandler(_make_handler(log_file))
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def get_scanner_logger(name: str, logs_dir: Path) -> logging.Logger:
    """
    Return a logger that writes to both console and logs/scan_premiums_YYYY-MM-DD.log.
    """
    _suppress_noise()
    logger = logging.getLogger(name)
    if not logger.handlers:
        # Console handler
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(message)s",
            datefmt="%H:%M:%S",
        ))
        logger.addHandler(console)
        # File handler
        log_file = logs_dir / f"scan_premiums_{_today()}.log"
        file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(message)s",
            datefmt="%H:%M:%S",
        ))
        logger.addHandler(file_handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger
