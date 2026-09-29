"""Website publish result contract compatible with Publishing channel adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.tools.publication_truth import TRUTH_UNPROVEN

CHANNEL_WEBSITE = "website"
OWNER_ENGINE = "Website Engine"

STATUS_RENDERED = "RENDERED"
STATUS_FAILED = "FAILED"


@dataclass
class WebsitePublishResult:
    """Frozen channel shape plus Website Engine detail fields."""

    ok: bool
    status: str
    message: str
    channel: str = CHANNEL_WEBSITE
    owner_engine: str = OWNER_ENGINE
    website_engine_invoked: bool = True
    rendering_performed: bool = False
    external_api_called: bool = False
    external_http: bool = False
    remote_write_acknowledged: bool = False
    remote_object_id: str = ""
    publication_truth: str = TRUTH_UNPROVEN
    slug: str = ""
    canonical_url: str = ""
    content_id: str = ""
    artifact_paths: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)

    def to_channel_result(self) -> dict[str, Any]:
        """Shape consumed by Publishing Engine website adapter boundary."""
        result: dict[str, Any] = {
            "ok": self.ok,
            "status": self.status,
            "channel": self.channel,
            "owner_engine": self.owner_engine,
            "message": self.message,
            "website_engine_invoked": self.website_engine_invoked,
            "rendering_performed": self.rendering_performed,
            "external_api_called": self.external_api_called,
            "external_http": self.external_http,
            "remote_write_acknowledged": self.remote_write_acknowledged,
            "remote_object_id": self.remote_object_id,
            "publication_truth": self.publication_truth,
        }
        if self.slug:
            result["slug"] = self.slug
        if self.canonical_url:
            result["canonical_url"] = self.canonical_url
        if self.content_id:
            result["content_id"] = self.content_id
        if self.artifact_paths:
            result["artifact_paths"] = dict(self.artifact_paths)
        if self.metadata:
            result["metadata"] = dict(self.metadata)
        if self.details:
            result["details"] = dict(self.details)
        return result


def success_result(
    *,
    message: str,
    content_id: str,
    slug: str,
    canonical_url: str,
    rendering_performed: bool,
    artifact_paths: dict[str, str] | None = None,
    metadata: dict[str, Any] | None = None,
    details: dict[str, Any] | None = None,
    status: str = STATUS_RENDERED,
) -> WebsitePublishResult:
    return WebsitePublishResult(
        ok=True,
        status=status,
        message=message,
        website_engine_invoked=True,
        rendering_performed=rendering_performed,
        external_api_called=False,
        external_http=False,
        remote_write_acknowledged=False,
        remote_object_id="",
        publication_truth=TRUTH_UNPROVEN,
        slug=slug,
        canonical_url=canonical_url,
        content_id=content_id,
        artifact_paths=artifact_paths or {},
        metadata=metadata or {},
        details=details or {},
    )


def failure_result(
    *,
    message: str,
    content_id: str = "",
    details: dict[str, Any] | None = None,
) -> WebsitePublishResult:
    return WebsitePublishResult(
        ok=False,
        status=STATUS_FAILED,
        message=message,
        website_engine_invoked=True,
        rendering_performed=False,
        external_api_called=False,
        external_http=False,
        remote_write_acknowledged=False,
        remote_object_id="",
        publication_truth=TRUTH_UNPROVEN,
        content_id=content_id,
        details=details or {},
    )
