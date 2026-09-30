"""Separate Content Ops readiness authorities.

Content review for the internal beta and autonomous publication are not the
same decision. This module does not infer publication readiness from a human
review surface, a QA pass, an editorial approval, or a publication job.
"""

from __future__ import annotations

READINESS_SURFACE_CONTENT_REVIEW = "content_review_internal_beta"


def autonomous_publication_ready() -> bool:
    """Autonomous publication stays fail-closed.

    A future gate may prove the publication contract. This function does not
    become true because internal beta review is usable.
    """
    return False
