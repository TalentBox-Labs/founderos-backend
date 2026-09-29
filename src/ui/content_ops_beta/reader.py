"""Select the Content Ops read port.

Normal Beta runtime uses the live current-week projection. The mock adapter
loads only when FOUNDER_OS_CONTENT_OPS_BETA_FIXTURE=1, or when a test installs
an override. A failed live read does not switch to the mock.
"""

from __future__ import annotations

import os

from src.ui.content_ops_beta.contract import ContentOpsReadPort
from src.ui.content_ops_beta.live_reader import LiveContentOpsReadAdapter
from src.ui.content_ops_beta.mock_adapter import MockContentOpsReadAdapter

FIXTURE_ENV = "FOUNDER_OS_CONTENT_OPS_BETA_FIXTURE"

_override: ContentOpsReadPort | None = None


def fixture_mode() -> bool:
    """Explicit local fixture switch. Staging and Beta leave this unset."""
    return os.environ.get(FIXTURE_ENV, "").strip() == "1"


def get_content_ops_reader() -> ContentOpsReadPort:
    if _override is not None:
        return _override
    if fixture_mode():
        return MockContentOpsReadAdapter()
    return LiveContentOpsReadAdapter()


def set_content_ops_reader(reader: ContentOpsReadPort | None) -> None:
    global _override
    _override = reader
