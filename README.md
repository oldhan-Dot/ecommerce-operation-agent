# ecommerce-operation-agent

电商运营智能助手。当前进入 M1 数据基础，已实现登录、账号管理、角色/店铺权限、店铺维护和审计；商品、日经营数据、CSV 导入与经营看板尚待后续 M1 交付。

## 已准备的环境

- 后端：Python 3.12、uv 管理的 backend/.venv、FastAPI、SQLAlchemy、PyMySQL、Alembic。
- 前端：Vue 3、TypeScript、Vite、Element Plus、Router、Pinia；ECharts 已安装待后续业务使用。
- 独立 Worker：当前只检查数据库状态，AI 执行属于 M2。
- 数据库：用户部署的云端 MySQL，使用 backend/.env 中的配置连接。
- 数据库迁移：`0002_identity` 创建身份、店铺、授权、会话与审计表，不提供默认管理员或默认密码。

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

此阶段对应 FR-01～FR-03 和基础审计。当前工作台显示实际权限及可访问店铺，不使用 UI 设计中的演示经营数值。完整 M1 仍需商品/SKU、竞品、经营数据、导入和看板模块；M2/M3 尚未交付。

## 文档

- [本机开发环境](docs/本机开发环境.md)：依赖重建、数据库配置、迁移与手动启动。
- [云端 MySQL 部署](docs/部署与本地开发准备.md)
- [PRD](docs/电商运营智能助手-PRD.md)
- [Spec](docs/电商运营智能助手-Spec.md)

根目录 .env 是云端 Compose 配置，本机 backend/.env 是后端配置；二者都不提交。重建依赖使用 backend 的 uv.lock 与 frontend 的 package-lock.json。

## 代码注释约定

新增和修改的代码、配置尽量使用中文注释，重点说明模块职责、业务规则、参数单位、安全边界、异常处理与并发约束；避免逐行重复代码含义。自动生成的锁文件、依赖包及不支持注释的 JSON 文件不手工添加注释。
