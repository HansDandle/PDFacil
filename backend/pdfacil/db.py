from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Font(Base):
    """A font file usable for rendering, indexed by PostScript name.

    Several rows may point at the same file (aliases created when a user maps an
    unrecognised name to a known font). owner is None for shared (Google) fonts.
    """

    __tablename__ = "fonts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    postscript_name: Mapped[str] = mapped_column(String(255))
    # Normalized name (see pipeline.fonts.font_key): "Open Sans Regular" == "OpenSans-Regular".
    lookup_key: Mapped[str] = mapped_column(String(255), index=True)
    family: Mapped[str] = mapped_column(String(255))
    weight: Mapped[int] = mapped_column(Integer, default=400)
    style: Mapped[str] = mapped_column(String(16), default="normal")
    license_note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    owner: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    object_key: Mapped[str] = mapped_column(String(512))
    source: Mapped[str] = mapped_column(String(16))  # google | upload | alias


class EditSession(Base):
    """A direct PDF edit session: the original file plus an ordered list of operations."""

    __tablename__ = "edit_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    filename: Mapped[str] = mapped_column(String(255))
    original_key: Mapped[str] = mapped_column(String(512))
    page_count: Mapped[int] = mapped_column(Integer)
    is_canva: Mapped[bool] = mapped_column(Boolean, default=False)
    ops: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite:///") and not url.startswith("sqlite:///:memory:"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url)
    Base.metadata.create_all(engine)  # replaced by Alembic migrations in the hosting milestone
    return engine


def make_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(engine, expire_on_commit=False)
