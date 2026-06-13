from datetime import datetime, date
from typing import Optional, Any
from uuid import UUID
from sqlalchemy import (
    BigInteger, Integer, String, Text, Boolean, Numeric, Date, DateTime,
    Float, ForeignKey, JSON, func
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

class Base(DeclarativeBase):
    pass


class Developer(Base):
    __tablename__ = "developers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    logo_url: Mapped[Optional[str]] = mapped_column(Text)
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    logo_s3_bucket: Mapped[Optional[str]] = mapped_column(Text)
    logo_s3_key: Mapped[Optional[str]] = mapped_column(Text)
    logo_s3_url: Mapped[Optional[str]] = mapped_column(Text)
    logo_mime_type: Mapped[Optional[str]] = mapped_column(Text)
    description: Mapped[Optional[str]] = mapped_column(Text)
    website: Mapped[Optional[str]] = mapped_column(Text)

    projects: Mapped[list["Project"]] = relationship(back_populates="developer", lazy="selectin")


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[Optional[str]] = mapped_column(Text)
    developer_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("developers.id"))
    country: Mapped[Optional[str]] = mapped_column(Text)
    region: Mapped[Optional[str]] = mapped_column(Text)
    district: Mapped[Optional[str]] = mapped_column(Text)
    lat: Mapped[Optional[float]] = mapped_column(Float)
    lng: Mapped[Optional[float]] = mapped_column(Float)
    min_price: Mapped[Optional[float]] = mapped_column(Numeric)
    max_price: Mapped[Optional[float]] = mapped_column(Numeric)
    currency: Mapped[Optional[str]] = mapped_column(Text)
    area_unit: Mapped[Optional[str]] = mapped_column(Text)
    min_size: Mapped[Optional[float]] = mapped_column(Numeric)
    max_size: Mapped[Optional[float]] = mapped_column(Numeric)
    sale_status: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[Optional[str]] = mapped_column(Text)
    completion_quarter: Mapped[Optional[str]] = mapped_column(Text)
    completion_date: Mapped[Optional[date]] = mapped_column(Date)
    has_escrow: Mapped[Optional[bool]] = mapped_column(Boolean)
    post_handover: Mapped[Optional[bool]] = mapped_column(Boolean)
    description: Mapped[Optional[str]] = mapped_column(Text)
    amenities: Mapped[Optional[Any]] = mapped_column(JSONB)
    overrides: Mapped[dict] = mapped_column(JSONB, nullable=False)
    is_published: Mapped[bool] = mapped_column(Boolean, nullable=False)
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False)
    reelly_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cover_image_url: Mapped[Optional[str]] = mapped_column(Text)
    short_description: Mapped[Optional[str]] = mapped_column(Text)
    managing_company: Mapped[Optional[str]] = mapped_column(Text)
    brand: Mapped[Optional[str]] = mapped_column(Text)
    construction_start_date: Mapped[Optional[date]] = mapped_column(Date)
    construction_end_date: Mapped[Optional[date]] = mapped_column(Date)
    completion_datetime: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    units_count: Mapped[Optional[int]] = mapped_column(Integer)
    building_count: Mapped[Optional[int]] = mapped_column(Integer)
    is_partner_project: Mapped[Optional[bool]] = mapped_column(Boolean)
    city: Mapped[Optional[str]] = mapped_column(Text)
    sector: Mapped[Optional[str]] = mapped_column(Text)
    escrow_number: Mapped[Optional[str]] = mapped_column(Text)
    service_charge: Mapped[Optional[str]] = mapped_column(Text)
    furnishing: Mapped[Optional[str]] = mapped_column(Text)
    deposit_description: Mapped[Optional[str]] = mapped_column(Text)
    readiness_progress: Mapped[Optional[float]] = mapped_column(Numeric)
    marketing_brochure_url: Mapped[Optional[str]] = mapped_column(Text)
    property_types: Mapped[Optional[Any]] = mapped_column(JSONB)

    developer: Mapped[Optional["Developer"]] = relationship(back_populates="projects", lazy="selectin")
    units: Mapped[list["ProjectUnit"]] = relationship(back_populates="project", lazy="selectin")
    assets: Mapped[list["ProjectAsset"]] = relationship(back_populates="project", lazy="selectin")


class ProjectUnit(Base):
    __tablename__ = "project_units"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("projects.id"), nullable=False)
    reelly_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    unit_type: Mapped[Optional[str]] = mapped_column(Text)
    bedrooms: Mapped[Optional[float]] = mapped_column(Numeric)
    price_from: Mapped[Optional[float]] = mapped_column(Numeric)
    price_to: Mapped[Optional[float]] = mapped_column(Numeric)
    size_from: Mapped[Optional[float]] = mapped_column(Numeric)
    size_to: Mapped[Optional[float]] = mapped_column(Numeric)
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False)
    unit_number: Mapped[Optional[str]] = mapped_column(Text)
    floor: Mapped[Optional[float]] = mapped_column(Numeric)
    bathrooms: Mapped[Optional[float]] = mapped_column(Numeric)
    view: Mapped[Optional[str]] = mapped_column(Text)
    orientation: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[Optional[str]] = mapped_column(Text)
    layout_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    building_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    currency: Mapped[Optional[str]] = mapped_column(Text)
    area_unit: Mapped[Optional[str]] = mapped_column(Text)
    plan_image_url: Mapped[Optional[str]] = mapped_column(Text)
    price: Mapped[Optional[float]] = mapped_column(Numeric)
    price_per_area: Mapped[Optional[float]] = mapped_column(Numeric)
    size: Mapped[Optional[float]] = mapped_column(Numeric)
    layout_name: Mapped[Optional[str]] = mapped_column(Text)
    layout_type: Mapped[Optional[str]] = mapped_column(Text)
    layout_images: Mapped[Optional[Any]] = mapped_column(JSONB)
    modified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    project: Mapped["Project"] = relationship(back_populates="units")


class ProjectAsset(Base):
    __tablename__ = "project_assets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("projects.id"), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)              # USER-DEFINED enum, treated as text
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_hash: Mapped[str] = mapped_column(Text, nullable=False)
    s3_bucket: Mapped[Optional[str]] = mapped_column(Text)
    s3_key: Mapped[Optional[str]] = mapped_column(Text)
    s3_url: Mapped[Optional[str]] = mapped_column(Text)
    filename: Mapped[Optional[str]] = mapped_column(Text)
    mime_type: Mapped[Optional[str]] = mapped_column(Text)
    size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    downloaded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    project: Mapped["Project"] = relationship(back_populates="assets")


class AskAlphaConversation(Base):
    __tablename__ = "ask_alpha_conversations"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    user_id: Mapped[Optional[UUID]] = mapped_column(PGUUID(as_uuid=True))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    project_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AskAlphaMessage(Base):
    __tablename__ = "ask_alpha_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("ask_alpha_conversations.id"), nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    cards: Mapped[Optional[Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Profile(Base):
    __tablename__ = "profiles"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    email: Mapped[Optional[str]] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ask_alpha_access: Mapped[str] = mapped_column(Text, nullable=False)
    first_name: Mapped[Optional[str]] = mapped_column(Text)
    last_name: Mapped[Optional[str]] = mapped_column(Text)
    phone: Mapped[Optional[str]] = mapped_column(Text)
    avatar_key: Mapped[Optional[str]] = mapped_column(Text)


class MessagingLink(Base):
    __tablename__ = "messaging_links"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    profile_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("profiles.id"), nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    requested_by: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("profiles.id"), nullable=False)
    project_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("projects.id"))
    script: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    heygen_video_id: Mapped[Optional[str]] = mapped_column(Text)
    video_url: Mapped[Optional[str]] = mapped_column(Text)
    thumbnail_url: Mapped[Optional[str]] = mapped_column(Text)
    error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    telegram_chat_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    # Reserved for a future caption/post-processing step — columns kept in the DB,
    # pipeline TBD (the Remotion implementation was removed). Nullable + unused for now.
    captioned_video_url: Mapped[Optional[str]] = mapped_column(Text)
    caption_status: Mapped[Optional[str]] = mapped_column(Text)
    caption_error: Mapped[Optional[str]] = mapped_column(Text)


class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("projects.id"))
    asset_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("project_assets.id"))
    source_kind: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
