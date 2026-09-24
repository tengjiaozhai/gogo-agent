"""Small deterministic tools for the first AgentScope exercise."""

from datetime import datetime


def get_current_date() -> str:
    """Return the current local calendar date in ISO 8601 format (YYYY-MM-DD)."""
    return datetime.now().astimezone().date().isoformat()
