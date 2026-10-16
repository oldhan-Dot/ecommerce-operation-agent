"""M1 账号、店铺、会话和审计模型；后续领域模型按独立迁移增加。"""

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import BIGINT, BINARY
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

ID = BigInteger().with_variant(BIGINT(unsigned=True), "mysql")


def utcnow() -> datetime:
    """数据库使用无时区的 UTC 时间，API 输出时再附加 UTC 标记。"""
    return datetime.now(UTC).replace(tzinfo=None)


class Record:
    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    created_by: Mapped[int | None] = mapped_column(ID, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class Enterprise(Record, Base):
    __tablename__ = "enterprises"
    name: Mapped[str] = mapped_column(String(100))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    currency: Mapped[str] = mapped_column(String(3), default="CNY")


class User(Record, Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("enterprise_id", "login_name", name="uq_user_login"),)
    enterprise_id: Mapped[int] = mapped_column(ForeignKey("enterprises.id", ondelete="RESTRICT"))
    login_name: Mapped[str] = mapped_column(String(64, collation="utf8mb4_bin"))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    password_version: Mapped[int] = mapped_column(Integer, default=1)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)


class Store(Record, Base):
    __tablename__ = "stores"
    __table_args__ = (
        UniqueConstraint("enterprise_id", "code", name="uq_store_code"),
        Index("ix_store_scope", "enterprise_id", "status", "id"),
    )
    enterprise_id: Mapped[int] = mapped_column(ForeignKey("enterprises.id", ondelete="RESTRICT"))
    code: Mapped[str] = mapped_column(String(64, collation="utf8mb4_bin"))
    name: Mapped[str] = mapped_column(String(100))
    platform: Mapped[str] = mapped_column(String(64))
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")


class StoreGrant(Record, Base):
    __tablename__ = "user_store_grants"
    __table_args__ = (UniqueConstraint("user_id", "store_id", name="uq_store_grant"),)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="RESTRICT"))
    granted_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    granted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Product(Record, Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("store_id", "code", name="uq_product_code"),
        UniqueConstraint("id", "store_id", name="uq_product_store"),
        Index("ix_product_scope", "store_id", "status", "id"),
    )
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="RESTRICT"))
    code: Mapped[str] = mapped_column(String(64, collation="utf8mb4_bin"))
    name: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(128))
    selling_points: Mapped[str] = mapped_column(Text)
    brand: Mapped[str | None] = mapped_column(String(100), nullable=True)
    audience: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")


class Sku(Record, Base):
    __tablename__ = "skus"
    __table_args__ = (
        UniqueConstraint("store_id", "code", name="uq_sku_code"),
        ForeignKeyConstraint(
            ["product_id", "store_id"],
            ["products.id", "products.store_id"],
            ondelete="RESTRICT",
            name="fk_sku_product_store",
        ),
    )
    store_id: Mapped[int] = mapped_column(ID)
    product_id: Mapped[int] = mapped_column(ID)
    code: Mapped[str] = mapped_column(String(64, collation="utf8mb4_bin"))
    spec_name: Mapped[str] = mapped_column(String(200))
    price: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)


class Competitor(Record, Base):
    __tablename__ = "competitors"
    __table_args__ = (
        UniqueConstraint("store_id", "code", name="uq_competitor_code"),
        ForeignKeyConstraint(
            ["product_id", "store_id"],
            ["products.id", "products.store_id"],
            name="fk_competitor_product_store",
            ondelete="RESTRICT",
        ),
        Index("ix_competitor_scope", "store_id", "status", "collected_at"),
    )
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="RESTRICT"))
    code: Mapped[str] = mapped_column(String(64, collation="utf8mb4_bin"))
    product_id: Mapped[int | None] = mapped_column(ID, nullable=True)
    name: Mapped[str] = mapped_column(String(200))
    platform: Mapped[str] = mapped_column(String(64))
    brand: Mapped[str | None] = mapped_column(String(100), nullable=True)
    spec: Mapped[str | None] = mapped_column(String(200), nullable=True)
    price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    selling_points: Mapped[str | None] = mapped_column(Text, nullable=True)
    excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_note: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    collected_at: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")


class AuthSession(Record, Base):
    __tablename__ = "auth_sessions"
    __table_args__ = (
        UniqueConstraint("token_digest", name="uq_session_digest"),
        Index("ix_session_user", "user_id", "revoked_at"),
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    token_digest: Mapped[bytes] = mapped_column(BINARY(32))
    csrf_digest: Mapped[bytes] = mapped_column(BINARY(32))
    password_version: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_time", "enterprise_id", "created_at", "id"),
        Index("ix_audit_target", "target_type", "target_id", "created_at"),
    )
    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    enterprise_id: Mapped[int] = mapped_column(ForeignKey("enterprises.id", ondelete="RESTRICT"))
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str] = mapped_column(String(64))
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[str] = mapped_column(String(64))
    result: Mapped[str] = mapped_column(String(16))
    redacted_diff: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
