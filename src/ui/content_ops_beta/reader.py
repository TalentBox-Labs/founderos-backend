"""UI read-port selector. Swap the adapter here without changing templates."""

from __future__ import annotations

from src.ui.content_ops_beta.contract import ContentOpsReadPort
from src.ui.content_ops_beta.mock_adapter import MockContentOpsReadAdapter

_override: ContentOpsReadPort | None = None


def get_content_ops_reader() -> ContentOpsReadPort:
    if _override is not None:
        return _override
    return MockContentOpsReadAdapter()


def set_content_ops_reader(reader: ContentOpsReadPort | None) -> None:
    global _override
    _override = reader
