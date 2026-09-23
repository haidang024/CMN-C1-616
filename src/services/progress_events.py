"""Optional Marketplace progress events that never gate domain execution."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def emit_progress(message: str, metadata: dict[str, Any]) -> None:
    """Emit progress when the Marketplace event transport is installed."""
    try:
        from shared.services.events import emitter
        from shared.services.events.types import EventType
    except (ImportError, ModuleNotFoundError):
        return
    try:
        emitter().emit_event(
            event_type=EventType.PROGRESS_UPDATE,
            message=message,
            metadata=metadata,
        )
    except Exception as exc:
        # Progress is advisory; transport failure must not suppress the
        # terminal graph response. Keep a safe diagnostic in Pod logs so a
        # broken event transport does not look like an agent that is thinking
        # forever.
        logger.warning("Marketplace progress event could not be delivered: %s", type(exc).__name__)
        return
