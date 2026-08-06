import json
import logging
import os

import config

logger = logging.getLogger(__name__)

_allowed: set[int] = set()


def _path() -> str:
    return config.WHITELIST_PATH


def load(extra_ids: list[int] | None = None) -> None:
    global _allowed
    _allowed = set(extra_ids or [])
    p = _path()
    if os.path.exists(p):
        try:
            with open(p) as f:
                _allowed.update(json.load(f))
            logger.info("Whitelist loaded from %s: %d users", p, len(_allowed))
        except Exception as e:
            logger.warning("Failed to load whitelist from %s: %s", p, e)


def _save() -> None:
    p = _path()
    try:
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "w") as f:
            json.dump(sorted(_allowed), f)
    except Exception as e:
        logger.warning("Failed to save whitelist to %s: %s", p, e)


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


def allowed_ids_env_value() -> str:
    """Return all current IDs as a comma-separated string for ALLOWED_USER_IDS env var."""
    return ",".join(str(i) for i in list_ids())
