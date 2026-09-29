from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from revenue_os.database import get_db
from revenue_os.models.content import ContentLibrary, SocialPost
from src.tools.publication_truth import local_claim_publication_truth

router = APIRouter(prefix="/social", tags=["social"])


def _reject_unproven_published_status(status: str | None) -> None:
    """A status token is not a remote CMS write."""
    if (status or "").strip().lower() != "published":
        return
    truth = local_claim_publication_truth(status)
    raise HTTPException(
        status_code=409,
        detail=(
            "Publication unproven. A status token cannot mark content published "
            f"(publication_truth={truth})."
        ),
    )


class SocialPostCreate(BaseModel):
    platform: str
    body: str
    media_urls: Optional[str] = None
    scheduled_at: Optional[datetime] = None
    content_library_id: Optional[str] = None


class SocialPostUpdate(BaseModel):
    body: Optional[str] = None
    scheduled_at: Optional[datetime] = None
    status: Optional[str] = None


class SocialPostResponse(BaseModel):
    id: uuid.UUID
    platform: str
    body: str
    media_urls: Optional[str] = None
    scheduled_at: Optional[datetime] = None
    published_at: Optional[datetime] = None
    status: str
    content_library_id: Optional[uuid.UUID] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ContentCreate(BaseModel):
    title: str
    content_type: str = "blog"
    body: Optional[str] = None
    tags: Optional[str] = None
    status: str = "draft"
    canonical_url: Optional[str] = None


class ContentUpdate(BaseModel):
    title: Optional[str] = None
    body: Optional[str] = None
    tags: Optional[str] = None
    status: Optional[str] = None


class ContentResponse(BaseModel):
    id: uuid.UUID
    title: str
    content_type: str
    body: Optional[str] = None
    tags: Optional[str] = None
    status: str
    canonical_url: Optional[str] = None
    published_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ---- Social Posts ----

@router.post("/posts", response_model=SocialPostResponse, status_code=201)
def create_post(body: SocialPostCreate, db: Session = Depends(get_db)):
    post = SocialPost(
        platform=body.platform,
        body=body.body,
        media_urls=body.media_urls,
        scheduled_at=body.scheduled_at,
        content_library_id=(
            uuid.UUID(body.content_library_id)
            if body.content_library_id
            else None
        ),
    )
    db.add(post)
    db.commit()
    db.refresh(post)
    return post


@router.get("/posts", response_model=list[SocialPostResponse])
def list_posts(
    platform: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    scheduled: Optional[bool] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    query = db.query(SocialPost)
    if platform:
        query = query.filter(SocialPost.platform == platform)
    if status:
        query = query.filter(SocialPost.status == status)
    if scheduled:
        query = query.filter(SocialPost.scheduled_at.isnot(None))
    return query.order_by(SocialPost.scheduled_at.desc().nullslast()).limit(limit).all()


@router.put("/posts/{post_id}", response_model=SocialPostResponse)
def update_post(
    post_id: str,
    body: SocialPostUpdate,
    db: Session = Depends(get_db),
):
    post = db.query(SocialPost).filter(SocialPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    update_data = body.model_dump(exclude_unset=True)
    _reject_unproven_published_status(update_data.get("status"))
    for key, value in update_data.items():
        if value is not None:
            setattr(post, key, value)
    db.commit()
    db.refresh(post)
    return post


@router.post("/posts/{post_id}/publish", response_model=SocialPostResponse)
def publish_post(post_id: str, db: Session = Depends(get_db)):
    post = db.query(SocialPost).filter(SocialPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    _reject_unproven_published_status("published")


# ---- Content Library ----

@router.post("/content", response_model=ContentResponse, status_code=201)
def create_content(body: ContentCreate, db: Session = Depends(get_db)):
    _reject_unproven_published_status(body.status)
    content = ContentLibrary(
        title=body.title,
        content_type=body.content_type,
        body=body.body,
        tags=body.tags,
        status=body.status,
        canonical_url=body.canonical_url,
    )
    db.add(content)
    db.commit()
    db.refresh(content)
    return content


@router.get("/content", response_model=list[ContentResponse])
def list_content(
    content_type: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    query = db.query(ContentLibrary)
    if content_type:
        query = query.filter(ContentLibrary.content_type == content_type)
    if status:
        query = query.filter(ContentLibrary.status == status)
    return query.order_by(ContentLibrary.updated_at.desc()).limit(limit).all()


@router.get("/content/{content_id}", response_model=ContentResponse)
def get_content(content_id: str, db: Session = Depends(get_db)):
    content = (
        db.query(ContentLibrary)
        .filter(ContentLibrary.id == content_id)
        .first()
    )
    if not content:
        raise HTTPException(status_code=404, detail="Content not found")
    return content
