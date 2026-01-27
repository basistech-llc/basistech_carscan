"""
Stub implementation of Brivo lookup.

This keeps the interface that the rest of the application expects
(`brivo_lookup(plate, state)`) but avoids making any real network or
Secrets Manager calls. Replace this function with a real implementation
when Brivo integration is ready.
"""

import logging
from typing import Final

LOGGER: Final = logging.getLogger(__name__)


def brivo_lookup(plate: str, state: str) -> str:
    """
    Stub Brivo lookup.

    Args:
        plate: License plate text (already upper‑cased by caller).
        state: Two‑letter state code.

    Returns:
        A display name string for the matched user, or ``\"Unknown\"``.
        For now this is a pure stub and always returns ``\"Unknown\"``.
    """
    LOGGER.debug("Brivo lookup stub called for plate=%s state=%s", plate, state)
    return "Unknown"

