"""Structured JSON logging with request/tenant context binding."""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)
tenant_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tenant_id", default=None
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id is not None:
            payload["request_id"] = request_id
        tenant_id = tenant_id_var.get()
        if tenant_id is not None:
            payload["tenant_id"] = tenant_id
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    for noisy in ("sqlalchemy.engine",):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def bind_tenant(tenant_id: str | None) -> None:
    tenant_id_var.set(tenant_id)
