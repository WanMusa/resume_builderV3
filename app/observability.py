"""Privacy-conscious structured operational events for stdout collection."""

import json
import logging
from datetime import datetime, timezone


_logger = logging.getLogger("resume_builder.events")


def emit_event(event: str, *, level: int = logging.INFO, **fields: object) -> None:
    record = {
        **fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": logging.getLevelName(level),
        "service": "resume-builder-v3",
        "event": event,
    }
    _logger.log(level, json.dumps(record, ensure_ascii=True, separators=(",", ":"), sort_keys=True))