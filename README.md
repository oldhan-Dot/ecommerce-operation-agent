# ecommerce-operation-agent

电商运营智能助手：单企业、多店铺，覆盖商品/SKU、竞品、三类日经营、CSV导入、指标看板、六类AI生成、不可变修订、人工审核和Markdown导出。管理员可配置DeepSeek、查询审计及运行监控。

主要代码及本机受控验收已完成；真实模型、云端容量及正式部署仍需外部环境验收，详见[剩余开发执行清单](docs/剩余开发执行清单.md)。模型结果始终为人工审核草稿，不自动上架、发布或投放广告。

## 已准备的环境

- 后端：Python 3.12、uv 管理的 backend/.venv、FastAPI、SQLAlchemy、PyMySQL、Alembic。
- 前端：Vue 3、TypeScript、Vite、Element Plus、Router、Pinia、ECharts。
- 独立 Worker：CSV预检/确认/清理、AI调度/生成/故障恢复、独立心跳与运维事件。
- 数据库：用户部署的云端 MySQL，使用 backend/.env 中的配置连接。
- 数据库迁移：`uv run alembic upgrade head`，当前为`0016_observation_indexes`；不提供默认管理员或默认密码。

## 优先使用直接命令启动

后端目录中安装依赖、执行迁移，并交互创建首个管理员：

```powershell
cd backend
uv sync --frozen
uv run alembic upgrade head
uv run python -m app.cli create-admin --username admin
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

管理员密码只在本机终端交互输入，不填写到命令参数、不提交到 GitHub。账号已存在时初始化命令会拒绝覆盖；首次初始化后不必每次运行。

另开后端终端启动Worker（任务生成及CSV处理必须运行）：

```powershell
uv run python -m app.worker
```

另开终端，在前端目录运行：

```powershell
cd frontend
npm ci --no-audit --no-fund
npm run dev
```

访问前端即可登录；管理员可以创建店铺、创建成员、分配店铺、重置密码及查看审计。普通成员首次登录必须修改临时密码。默认前端地址 `http://127.0.0.1:5173`，使用浏览器同源代理连接 API。

## 可选：已有 Windows 启停辅助命令

下列脚本保留供已有环境使用，后续开发优先使用上面的直接命令。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-dev.ps1
```

- 前端：http://127.0.0.1:5173
- API文档：http://127.0.0.1:8000/api/docs
- 后端就绪：http://127.0.0.1:8000/api/v1/health/ready

停止本项目服务：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\stop-dev.ps1
```

日志保存在 .local，配置修改后重启服务。Python 依赖隔离在项目内，不需要修改全局 Python 环境。

## 测试与阶段范围

在 backend 执行 `uv run ruff check .`、`uv run pytest`。显式运行真实 MySQL 测试：

```powershell
$env:RUN_MYSQL_TESTS='1'
uv run pytest -q
```

`backend/tests/test_identity.py` 是身份权限集成测试脚本，文件开头标明用途、运行命令、前置条件和事务回滚隔离规则。前端运行 `npm run build` 验证类型与构建。

完整MySQL测试含权限反向、唯一键/事务回滚、多连接并发、故障恢复及生成契约验证。浏览器脚本和备份恢复演练均在文件开头说明独立测试环境要求，禁止指向生产库。GitHub Actions使用MySQL8.4执行迁移、检查和完整回归，并构建前端。

模型使用：管理员在“模型配置”填写Key并测试连接；运营人员在“AI工作台”选择类型和来源生成。六类分别为商品分析、竞品分析、主题创意、视频脚本、投放建议、经营复盘。作者在“产出与审核”创建修订并提交，另一名管理员/主管审核后可正式导出。没有另一名审核人时不能自审。

## 文档

- [本机开发环境](docs/本机开发环境.md)：依赖重建、数据库配置、迁移与手动启动。
- [云端 MySQL 部署](docs/部署与本地开发准备.md)
- [PRD](docs/电商运营智能助手-PRD.md)
- [Spec](docs/电商运营智能助手-Spec.md)
- [审核验收记录](docs/M3产出审核验收记录.md)
- [运行监控与故障定位](docs/运行监控与故障定位.md)
- [应用发布与备份恢复](docs/应用发布与备份恢复.md)
- [总体验收矩阵](docs/总体验收矩阵.md)

根目录 .env 是云端 Compose 配置，本机 backend/.env 是后端配置；二者都不提交。重建依赖使用 backend 的 uv.lock 与 frontend 的 package-lock.json。

## 代码注释约定

新增和修改的代码、配置尽量使用中文注释，重点说明模块职责、业务规则、参数单位、安全边界、异常处理与并发约束；避免逐行重复代码含义。自动生成的锁文件、依赖包及不支持注释的 JSON 文件不手工添加注释。
