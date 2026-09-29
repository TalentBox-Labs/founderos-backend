"""Website Engine orchestration entrypoints (site publishing only).

Consumes editorial filesystem bundles / Publishing job dicts.
Does not own Publishing state machine, social, email, or campaigns.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.tools.runtime_paths import REPO_ROOT
from src.tools.website_engine.content_model import WebsiteContent, load_website_content
from src.tools.website_engine.metadata import WebsiteMetadata, build_metadata
from src.tools.website_engine.provider import (
    WebsiteProvider,
    WebsitePublicationRequest,
)
from src.tools.website_engine.publish_result import (
    WebsitePublishResult,
    failure_result,
    success_result,
)
from src.tools.website_engine.registry import get_provider
from src.tools.website_engine.render import RenderResult, render_markdown
from src.tools.website_engine.urls import build_canonical_url, build_slug

DEFAULT_PROVIDER_NAME = "static"


def prepare_website_page(
    content: WebsiteContent,
    *,
    site_base: str | None = None,
) -> tuple[str, str, WebsiteMetadata, RenderResult]:
    """Build slug, canonical URL, metadata, and HTML for a content bundle."""
    slug = build_slug(
        title=content.title,
        primary_keyword=content.primary_keyword,
        front_matter=content.front_matter,
    )
    kwargs: dict[str, Any] = {"front_matter": content.front_matter}
    if site_base is not None:
        kwargs["site_base"] = site_base
    canonical_url = build_canonical_url(slug, **kwargs)
    metadata = build_metadata(
        title=content.title,
        description=content.description_hint or content.title,
        canonical_url=canonical_url,
        slug=slug,
        primary_keyword=content.primary_keyword,
    )
    rendered = render_markdown(content.markdown_body)
    return slug, canonical_url, metadata, rendered


def publish_content(
    content_id: str,
    *,
    bundle: str | None = None,
    repo_root: Path | None = None,
    provider: WebsiteProvider | None = None,
    provider_name: str | None = None,
    site_base: str | None = None,
    output_dir: Path | None = None,
) -> WebsitePublishResult:
    """Load bundle → render → publish via static (default) provider. No external HTTP."""
    root = repo_root or REPO_ROOT
    try:
        content = load_website_content(
            content_id, bundle=bundle, repo_root=root
        )
    except (OSError, ValueError) as exc:
        return failure_result(
            message=f"Website Engine failed to load content: {exc}",
            content_id=(content_id or "").strip().upper(),
            details={"error": str(exc)},
        )

    slug, canonical_url, metadata, rendered = prepare_website_page(
        content, site_base=site_base
    )
    try:
        active_provider = _resolve_provider(
            provider=provider,
            provider_name=provider_name,
            output_dir=output_dir,
        )
    except KeyError as exc:
        return failure_result(
            message=f"Website Engine unknown provider: {exc}",
            content_id=content.content_id,
            details={"error": str(exc)},
        )

    request = WebsitePublicationRequest(
        content_id=content.content_id,
        slug=slug,
        canonical_url=canonical_url,
        title=content.title,
        html=rendered.html,
        markdown=content.markdown_body,
        metadata=metadata,
        render=rendered,
    )
    provider_result = active_provider.publish(request)
    if not provider_result.ok:
        return failure_result(
            message=provider_result.message,
            content_id=content.content_id,
            details=provider_result.to_dict(),
        )

    return success_result(
        message=(
            "Website Engine rendered Markdown to HTML and wrote local artifacts "
            f"for {content.content_id} via {provider_result.provider}. "
            "Local artifact writes are not remote publication."
        ),
        content_id=content.content_id,
        slug=slug,
        canonical_url=canonical_url,
        rendering_performed=True,
        artifact_paths=provider_result.artifact_paths,
        metadata=metadata.to_dict(),
        details={
            "provider": provider_result.to_dict(),
            "bundle_path": content.bundle_path,
            "source_final_path": content.source_final_path,
            "renderer": rendered.renderer,
        },
    )


def publish_from_job(
    job: dict[str, Any],
    *,
    repo_root: Path | None = None,
    provider: WebsiteProvider | None = None,
    provider_name: str | None = None,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Website channel entrypoint: accept Publishing job dict → channel result.

    Safe to call from ``_adapter_website`` without expanding Publishing API.
    Does not mutate job state (Publishing Engine owns the state machine).
    """
    content_id = str(job.get("content_id") or "").strip()
    bundle = job.get("bundle")
    bundle_str = str(bundle) if bundle else None
    name = provider_name
    if name is None and job.get("provider"):
        name = str(job.get("provider"))
    result = publish_content(
        content_id,
        bundle=bundle_str,
        repo_root=repo_root,
        provider=provider,
        provider_name=name,
        output_dir=output_dir,
    )
    return result.to_channel_result()


def _resolve_provider(
    *,
    provider: WebsiteProvider | None,
    provider_name: str | None,
    output_dir: Path | None,
) -> WebsiteProvider:
    if provider is not None:
        return provider
    name = provider_name or DEFAULT_PROVIDER_NAME
    return get_provider(name, output_dir=output_dir)
