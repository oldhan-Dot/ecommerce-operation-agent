"""M1 身份权限与店铺：显式创建业务表，不初始化默认账号或密码。"""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision = "0002_identity"
down_revision = "0001_environment"
branch_labels = None
depends_on = None


def identity_columns():
    """当前迁移固定的公共字段，避免未来模型变化影响历史迁移。"""
    return [
        sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
    ]


def upgrade():
    op.create_table(
        "enterprises",
        *identity_columns(),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_table(
        "users",
        *identity_columns(),
        sa.Column(
            "enterprise_id",
            mysql.BIGINT(unsigned=True),
            sa.ForeignKey("enterprises.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("login_name", sa.String(64, collation="utf8mb4_bin"), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("locked_until", sa.DateTime(), nullable=True),
        sa.Column("password_version", sa.Integer(), nullable=False),
        sa.Column("must_change_password", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("enterprise_id", "login_name", name="uq_user_login"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_table(
        "stores",
        *identity_columns(),
        sa.Column(
            "enterprise_id",
            mysql.BIGINT(unsigned=True),
            sa.ForeignKey("enterprises.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("code", sa.String(64, collation="utf8mb4_bin"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("platform", sa.String(64), nullable=False),
        sa.Column("external_id", sa.String(128), nullable=True),
        sa.Column("category", sa.String(128), nullable=True),
        sa.Column("notes", sa.String(2000), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.UniqueConstraint("enterprise_id", "code", name="uq_store_code"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_store_scope", "stores", ["enterprise_id", "status", "id"])
    op.create_table(
        "user_store_grants",
        *identity_columns(),
        sa.Column(
            "user_id",
            mysql.BIGINT(unsigned=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "store_id",
            mysql.BIGINT(unsigned=True),
            sa.ForeignKey("stores.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "granted_by",
            mysql.BIGINT(unsigned=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("granted_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "store_id", name="uq_store_grant"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_table(
        "auth_sessions",
        *identity_columns(),
        sa.Column(
            "user_id",
            mysql.BIGINT(unsigned=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("token_digest", mysql.BINARY(32), nullable=False),
        sa.Column("csrf_digest", mysql.BINARY(32), nullable=False),
        sa.Column("password_version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("token_digest", name="uq_session_digest"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_session_user", "auth_sessions", ["user_id", "revoked_at"])
    op.create_table(
        "audit_events",
        sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column(
            "enterprise_id",
            mysql.BIGINT(unsigned=True),
            sa.ForeignKey("enterprises.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "actor_id", mysql.BIGINT(unsigned=True), sa.ForeignKey("users.id"), nullable=True
        ),
        sa.Column(
            "store_id", mysql.BIGINT(unsigned=True), sa.ForeignKey("stores.id"), nullable=True
        ),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(64), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=True),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("result", sa.String(16), nullable=False),
        sa.Column("redacted_diff", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_audit_time", "audit_events", ["enterprise_id", "created_at", "id"])
    op.create_index("ix_audit_target", "audit_events", ["target_type", "target_id", "created_at"])


def downgrade():
    # 只在人工确认回退时使用；依外键顺序移除本阶段表，日常重启不得执行。
    for name in (
        "audit_events",
        "auth_sessions",
        "user_store_grants",
        "stores",
        "users",
        "enterprises",
    ):
        op.drop_table(name)
