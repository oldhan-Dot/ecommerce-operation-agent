"""环境基线：只记录迁移版本，业务表随后续功能通过新迁移创建。"""

revision = "0001_environment"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 不在环境初始化阶段提前建立尚未实现的业务表。
    pass


def downgrade() -> None:
    # 当前版本无业务表变更，因此无需执行业务结构回退。
    pass
