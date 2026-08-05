import json
import logging
import os

WHITELIST_FILE = "whitelist.json"

logger = logging.getLogger(__name__)

_allowed: set[int] = set()


def load(extra_ids: list[int] | None = None) -> None:
    global _allowed
    _allowed = set(extra_ids or [])
    if os.path.exists(WHITELIST_FILE):
        try:
            with open(WHITELIST_FILE) as f:
                _allowed.update(json.load(f))
            logger.info("Whitelist loaded: %d users", len(_allowed))
        except Exception as e:
            logger.warning("Failed to load whitelist: %s", e)


def _save() -> None:
    try:
        with open(WHITELIST_FILE, "w") as f:
            json.dump(sorted(_allowed), f)
    except Exception as e:
        logger.warning("Failed to save whitelist: %s", e)


def add(user_id: int) -> None:
    _allowed.add(user_id)
    _save()


def remove(user_id: int) -> None:
    _allowed.discard(user_id)
    _save()


def is_allowed(user_id: int, admin_id: int) -> bool:
    return user_id == admin_id or user_id in _allowed


def list_ids() -> list[int]:
    return sorted(_allowed)
