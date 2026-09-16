"""Outbox worker entry point: ``python -m app.worker [--once]``."""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.config import get_settings
from app.db import SessionLocal
from app.logging_config import configure_logging, get_logger
from app.services.outbox import process_batch, run_forever, worker_identity

logger = get_logger("app.worker")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Outbox event worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="claim and process at most one batch, then exit (useful for tests/demos)",
    )
    return parser.parse_args()


async def _main() -> int:
    args = parse_args()
    settings = get_settings()
    configure_logging(settings.log_level)
    worker_id = worker_identity()

    if args.once:
        from app.services.outbox import default_sender

        stats = await process_batch(SessionLocal, worker_id, default_sender, settings)
        logger.info(
            "worker --once finished",
            extra={"extra_fields": {"worker_id": worker_id, **stats.__dict__}},
        )
        return 0

    await run_forever(SessionLocal, settings, worker_id=worker_id)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_main()))
    except KeyboardInterrupt:  # pragma: no cover
        sys.exit(0)
