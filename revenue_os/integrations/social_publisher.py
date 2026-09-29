"""
Social media publisher clients for WorkCrew / HireStack / Founder content.

Supports:
  - Hashnode (blog)        — GraphQL API
  - LinkedIn               — REST API v2 (UGC Posts)
  - Instagram              — Facebook Graph API (requires Business account)
  - YouTube                — YouTube Data API v3

All credentials are read from environment variables (see .env.example).
No credentials are hardcoded. Publish calls are gated behind
explicit `confirmed=True` flags to prevent accidental publishing.

Usage:
    from revenue_os.integrations.social_publisher import SocialPublisher
    pub = SocialPublisher()
    result = pub.publish_all(status_path="output/marketing/workcrew/slug/08_Publish_Status.json")
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from src.tools.publication_truth import (
    TRUTH_REMOTE_WRITE_CONFIRMED,
    TRUTH_UNKNOWN_REMOTE,
    apply_provider_body_to_channel_status,
    channel_rewrite_blocked,
    mark_channel_remote_unknown,
)

ROOT = Path(__file__).resolve().parents[2]

# ── Env helpers ──────────────────────────────────────────────────────────────


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _require_env(key: str) -> str:
    v = _env(key)
    if not v:
        raise EnvironmentError(
            f"Missing required environment variable: {key}\n"
            f"Add it to your .env file. See .env.example for details."
        )
    return v


def _post_json(url: str, payload: dict, headers: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} from {url}: {body}") from e


def _write_status(path: Path, status: dict[str, Any]) -> None:
    path.write_text(json.dumps(status, indent=2), encoding="utf-8")


def _unknown_remote_result() -> dict[str, Any]:
    return {
        "ok": False,
        "publication_truth": TRUTH_UNKNOWN_REMOTE,
        "remote_write_outstanding": True,
    }


def _blocked_rewrite_result() -> dict[str, Any]:
    return {
        "skipped": True,
        "reason": "unknown remote result; rewrite blocked",
        "publication_truth": TRUTH_UNKNOWN_REMOTE,
    }


def _invoke_confirmed_channel(
    status: dict[str, Any],
    status_path: Path,
    channel: str,
    call: Callable[[], Any],
    *,
    confirmed: bool,
    url_field: str | None = None,
) -> dict[str, Any]:
    """Run one provider call. Unknown transport failures do not schedule a rewrite."""
    if channel_rewrite_blocked(status, channel):
        return _blocked_rewrite_result()
    published = status.get("published") or {}
    if isinstance(published, dict) and published.get(channel):
        return {"skipped": True, "reason": "already published"}
    try:
        result = call()
    except urllib.error.URLError:
        mark_channel_remote_unknown(status, channel)
        _write_status(status_path, status)
        return _unknown_remote_result()
    if not isinstance(result, dict):
        result = {"ok": False, "status": "FAILED", "message": "Provider returned a non-object"}
    if confirmed:
        truth = apply_provider_body_to_channel_status(status, channel, result)
        if truth != TRUTH_REMOTE_WRITE_CONFIRMED:
            # The request returned without a remote object id. Do not write again.
            mark_channel_remote_unknown(status, channel)
        elif url_field:
            status[url_field] = result.get("url", "")
        _write_status(status_path, status)
    return result


def _get_json(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


# ── Hashnode ─────────────────────────────────────────────────────────────────


class HashnodePublisher:
    """Publish a blog article to Hashnode via GraphQL."""

    GQL = "https://gql.hashnode.com/graphql"

    MUTATION = """
    mutation PublishPost($input: PublishPostInput!) {
      publishPost(input: $input) {
        post { id title slug url canonicalUrl }
      }
    }
    """

    def __init__(self) -> None:
        self.token      = _require_env("HASHNODE_ACCESS_TOKEN")
        self.pub_id     = _require_env("HASHNODE_PUBLICATION_ID")
        self.endpoint   = _env("HASHNODE_GQL_ENDPOINT", self.GQL)

    def _gql(self, query: str, variables: dict) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Authorization": self.token,
        }
        return _post_json(self.endpoint, {"query": query, "variables": variables}, headers)

    @staticmethod
    def _parse_front_matter(text: str) -> tuple[dict, str]:
        if not text.lstrip().startswith("---"):
            return {}, text
        lines = text.splitlines()
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if end is None:
            return {}, text
        import yaml
        fm = yaml.safe_load("\n".join(lines[1:end])) or {}
        body = "\n".join(lines[end + 1:])
        return fm, body

    def publish(self, article_path: str, *, confirmed: bool = False, dry_run: bool = False) -> dict:
        text = (ROOT / article_path).read_text(encoding="utf-8")
        fm, body = self._parse_front_matter(text)
        title = fm.get("title") or fm.get("article_title", "Untitled")
        slug  = re.sub(r"[^a-z0-9]+", "-", title.lower().strip()).strip("-")
        canonical = fm.get("canonical_url", "")

        payload: dict[str, Any] = {
            "publicationId": self.pub_id,
            "title": title,
            "contentMarkdown": body,
            "slug": slug,
        }
        if canonical:
            payload["originalArticleURL"] = canonical

        if dry_run:
            return {"dry_run": True, "title": title, "slug": slug, "payload_keys": list(payload)}

        if not confirmed:
            raise RuntimeError(
                "Hashnode publish requires confirmed=True. "
                "Review the article first, then call with confirmed=True."
            )

        result = self._gql(self.MUTATION, {"input": payload})
        return result.get("data", {}).get("publishPost", {}).get("post", result)


# ── LinkedIn ─────────────────────────────────────────────────────────────────


class LinkedInPublisher:
    """Post text content to LinkedIn via API v2 (UGC Posts)."""

    API = "https://api.linkedin.com/v2"

    def __init__(self) -> None:
        self.token   = _require_env("LINKEDIN_ACCESS_TOKEN")
        self.person_id = _env("LINKEDIN_PERSON_URN", "")  # urn:li:person:XXXXXX

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "X-Restli-Protocol-Version": "2.0.0",
        }

    def _person_urn(self) -> str:
        if self.person_id:
            return self.person_id
        me = _get_json(f"{self.API}/me", self._headers())
        return f"urn:li:person:{me['id']}"

    def publish(self, post_text: str, *, article_url: str = "", confirmed: bool = False, dry_run: bool = False) -> dict:
        urn = self._person_urn()
        # Truncate to LinkedIn's 3000-char limit
        body = post_text[:3000]
        if article_url:
            body = body.rstrip() + f"\n\n{article_url}"

        payload: dict[str, Any] = {
            "author": urn,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": body},
                    "shareMediaCategory": "NONE",
                }
            },
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
        }

        if dry_run:
            return {"dry_run": True, "author": urn, "chars": len(body)}

        if not confirmed:
            raise RuntimeError("LinkedIn publish requires confirmed=True.")

        return _post_json(f"{self.API}/ugcPosts", payload, self._headers())


# ── Instagram ────────────────────────────────────────────────────────────────


class InstagramPublisher:
    """
    Publish image+caption to Instagram Business via Facebook Graph API.

    Requires:
      INSTAGRAM_ACCESS_TOKEN  — long-lived Page access token
      INSTAGRAM_ACCOUNT_ID    — numeric Instagram Business account ID
    """

    GRAPH = "https://graph.facebook.com/v19.0"

    def __init__(self) -> None:
        self.token      = _require_env("INSTAGRAM_ACCESS_TOKEN")
        self.account_id = _require_env("INSTAGRAM_ACCOUNT_ID")

    def _post(self, endpoint: str, payload: dict) -> dict:
        headers = {"Content-Type": "application/json"}
        payload["access_token"] = self.token
        return _post_json(f"{self.GRAPH}/{endpoint}", payload, headers)

    def publish(
        self,
        *,
        caption: str,
        image_url: str,
        confirmed: bool = False,
        dry_run: bool = False,
    ) -> dict:
        """
        Two-step Instagram publish: create media container → publish container.
        image_url must be a publicly accessible URL (CDN or S3).
        """
        caption = caption[:2200]  # Instagram caption limit

        if dry_run:
            return {"dry_run": True, "account_id": self.account_id, "caption_chars": len(caption)}

        if not confirmed:
            raise RuntimeError("Instagram publish requires confirmed=True.")

        # Step 1: Create media container
        container = self._post(
            f"{self.account_id}/media",
            {"image_url": image_url, "caption": caption},
        )
        container_id = container.get("id")
        if not container_id:
            raise RuntimeError(f"Failed to create Instagram media container: {container}")

        # Step 2: Publish container
        result = self._post(
            f"{self.account_id}/media_publish",
            {"creation_id": container_id},
        )
        return result

    def publish_reel(
        self,
        *,
        caption: str,
        video_url: str,
        cover_url: str = "",
        confirmed: bool = False,
        dry_run: bool = False,
    ) -> dict:
        """Upload a short-form Reel video."""
        if dry_run:
            return {"dry_run": True, "type": "reel"}

        if not confirmed:
            raise RuntimeError("Instagram Reel publish requires confirmed=True.")

        payload: dict[str, Any] = {
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption[:2200],
        }
        if cover_url:
            payload["cover_url"] = cover_url

        container = self._post(f"{self.account_id}/media", payload)
        container_id = container.get("id")
        if not container_id:
            raise RuntimeError(f"Failed to create Reel container: {container}")

        return self._post(f"{self.account_id}/media_publish", {"creation_id": container_id})


# ── YouTube ──────────────────────────────────────────────────────────────────


class YouTubePublisher:
    """
    Upload a video description/metadata record to YouTube via Data API v3.

    Note: Actual video file upload requires OAuth2 flow — this client handles
    the metadata insert (title, description, tags, category). Video file upload
    is handled by a separate OAuth-authenticated session (see docs).

    Requires:
      YOUTUBE_API_KEY          — for metadata operations
      YOUTUBE_CHANNEL_ID       — target channel
    """

    API = "https://www.googleapis.com/youtube/v3"

    def __init__(self) -> None:
        self.api_key    = _require_env("YOUTUBE_API_KEY")
        self.channel_id = _env("YOUTUBE_CHANNEL_ID", "")

    def create_video_metadata(
        self,
        *,
        title: str,
        description: str,
        tags: list[str],
        category_id: str = "22",  # 22 = People & Blogs, 28 = Science & Technology
        confirmed: bool = False,
        dry_run: bool = False,
    ) -> dict:
        """
        Prepare YouTube video metadata payload.
        Returns the payload for review or posts it if confirmed=True.
        """
        payload = {
            "snippet": {
                "title": title[:100],
                "description": description[:5000],
                "tags": tags[:500],
                "categoryId": category_id,
            },
            "status": {"privacyStatus": "private"},  # Always private until human review
        }

        if dry_run:
            return {"dry_run": True, "title": title, "tag_count": len(tags)}

        if not confirmed:
            raise RuntimeError(
                "YouTube metadata create requires confirmed=True. "
                "Video privacy is set to private until you manually publish."
            )

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        return _post_json(f"{self.API}/videos?part=snippet,status", payload, headers)


# ── Orchestrator ─────────────────────────────────────────────────────────────


class SocialPublisher:
    """
    High-level publisher that reads a marketing 08_Publish_Status.json manifest
    and publishes each channel based on what's present and what's already done.
    """

    def __init__(self) -> None:
        self._hashnode  = None
        self._linkedin  = None
        self._instagram = None
        self._youtube   = None

    def _blog(self) -> HashnodePublisher:
        if not self._hashnode:
            self._hashnode = HashnodePublisher()
        return self._hashnode

    def _li(self) -> LinkedInPublisher:
        if not self._linkedin:
            self._linkedin = LinkedInPublisher()
        return self._linkedin

    def _ig(self) -> InstagramPublisher:
        if not self._instagram:
            self._instagram = InstagramPublisher()
        return self._instagram

    def _yt(self) -> YouTubePublisher:
        if not self._youtube:
            self._youtube = YouTubePublisher()
        return self._youtube

    @staticmethod
    def _load_status(status_path: str) -> tuple[dict, Path]:
        p = ROOT / status_path
        if not p.is_file():
            raise FileNotFoundError(f"Publish status not found: {p}")
        return json.loads(p.read_text(encoding="utf-8")), p

    @staticmethod
    def _artifact_text(artifacts: dict, key: str) -> str:
        rel = artifacts.get(key, "")
        if not rel:
            return ""
        p = ROOT / rel
        return p.read_text(encoding="utf-8") if p.is_file() else ""

    def dry_run(self, status_path: str) -> dict:
        """Preview what would be published without making any API calls."""
        status, _ = self._load_status(status_path)
        arts = status.get("artifacts", {})
        return {
            "brand":   status.get("brand"),
            "topic":   status.get("topic"),
            "keyword": status.get("target_keyword"),
            "channels": {
                "hashnode":  bool(self._artifact_text(arts, "02_Blog_Article.md")),
                "linkedin":  bool(self._artifact_text(arts, "03_LinkedIn_Post.md")),
                "instagram": bool(self._artifact_text(arts, "04_Instagram_Post.md")),
                "youtube":   bool(self._artifact_text(arts, "05_YouTube_Content.md")),
            },
            "already_published": status.get("published", {}),
        }

    def publish_blog(self, status_path: str, *, confirmed: bool = False) -> dict:
        status, sp = self._load_status(status_path)
        article_rel = status.get("artifacts", {}).get("02_Blog_Article.md", "")
        return _invoke_confirmed_channel(
            status,
            sp,
            "hashnode",
            lambda: self._blog().publish(article_rel, confirmed=confirmed),
            confirmed=confirmed,
            url_field="hashnode_url",
        )

    def publish_linkedin(self, status_path: str, *, confirmed: bool = False) -> dict:
        status, sp = self._load_status(status_path)
        arts = status.get("artifacts", {})
        post_text = self._artifact_text(arts, "03_LinkedIn_Post.md")
        article_url = status.get("canonical_url", "") or status.get("hashnode_url", "")
        return _invoke_confirmed_channel(
            status,
            sp,
            "linkedin",
            lambda: self._li().publish(
                post_text, article_url=article_url, confirmed=confirmed
            ),
            confirmed=confirmed,
        )

    def publish_instagram(
        self,
        status_path: str,
        *,
        image_url: str,
        confirmed: bool = False,
    ) -> dict:
        status, sp = self._load_status(status_path)
        arts = status.get("artifacts", {})
        caption_raw = self._artifact_text(arts, "04_Instagram_Post.md")
        caption_match = re.search(
            r"## Instagram Caption\s*\n(.*?)(?=\n## |\Z)", caption_raw, re.DOTALL
        )
        caption = caption_match.group(1).strip() if caption_match else caption_raw[:2200]
        return _invoke_confirmed_channel(
            status,
            sp,
            "instagram",
            lambda: self._ig().publish(
                caption=caption, image_url=image_url, confirmed=confirmed
            ),
            confirmed=confirmed,
        )

    def publish_youtube_metadata(
        self,
        status_path: str,
        *,
        confirmed: bool = False,
    ) -> dict:
        status, sp = self._load_status(status_path)
        arts = status.get("artifacts", {})
        yt_text = self._artifact_text(arts, "05_YouTube_Content.md")
        title_match = re.search(r"## YouTube Titles\s*\n(.+?)(?:\n|$)", yt_text)
        title = (
            title_match.group(1).strip().lstrip("- ")
            if title_match
            else status.get("topic", "")
        )
        desc_match = re.search(r"## Description\s*\n(.*?)(?=\n## |\Z)", yt_text, re.DOTALL)
        description = desc_match.group(1).strip() if desc_match else ""
        tags_match = re.search(r"## Tags\s*\n(.*?)(?=\n## |\Z)", yt_text, re.DOTALL)
        tags = [
            tag.strip().lstrip("- ")
            for tag in (tags_match.group(1).splitlines() if tags_match else [])
            if tag.strip()
        ]
        return _invoke_confirmed_channel(
            status,
            sp,
            "youtube",
            lambda: self._yt().create_video_metadata(
                title=title, description=description, tags=tags, confirmed=confirmed
            ),
            confirmed=confirmed,
        )

    def publish_all(
        self,
        status_path: str,
        *,
        instagram_image_url: str = "",
        confirmed: bool = False,
    ) -> dict:
        """Publish to all channels in sequence. Returns per-channel results."""
        results: dict[str, Any] = {}

        for channel, fn in [
            ("hashnode",  lambda: self.publish_blog(status_path, confirmed=confirmed)),
            ("linkedin",  lambda: self.publish_linkedin(status_path, confirmed=confirmed)),
            ("youtube",   lambda: self.publish_youtube_metadata(status_path, confirmed=confirmed)),
        ]:
            try:
                results[channel] = fn()
            except Exception as e:
                results[channel] = {"error": str(e)}

        if instagram_image_url:
            try:
                results["instagram"] = self.publish_instagram(
                    status_path, image_url=instagram_image_url, confirmed=confirmed
                )
            except Exception as e:
                results["instagram"] = {"error": str(e)}
        else:
            results["instagram"] = {"skipped": True, "reason": "no image_url provided — generate image first"}

        return results
