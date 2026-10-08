# 从空环境复现

## 本机 Python + Docker 数据库

先安装 Python 3.11+（建议 3.12）和 Docker，启动 Docker。使用项目 README 中的 venv 与依赖安装命令。从项目根目录执行：

```sh
docker compose up -d --wait vectors
python scripts/demo.py init
python scripts/demo.py serve
```

数据库绑定 `127.0.0.1:55432`，API 默认 `127.0.0.1:8000`。Compose 已包含数据库健康检查。`init` 必须在 `serve` 前运行；不依赖本地历史 SQLite、迁移报告、私人笔记或真实模型密钥。内置数据库账号和密码只用于此本地演示。

演示固定使用 Fake Embedding / Generation / Agent 和 hybrid 检索，不会继承当前终端的模型密钥。样例在 `ko_demo` schema，与已有 public 或 ko_samples 数据分开。初始化不删表，但显式准备样例 ACL / 业务权限；不要在用户数据 schema 中执行。上传、切块、索引仍是独立阶段。

使用其他本地 PostgreSQL 时，服务器需要 pgvector 扩展（共享安装在 public schema），初始化账号需要 CREATE SCHEMA、CREATE EXTENSION、创建本地只读角色等权限。将连接设置为环境变量后执行同样的命令：

```powershell
$env:KNOWLEDGEOPS_DATABASE_URL='postgresql://USER:PASSWORD@127.0.0.1:PORT/DB'
python scripts/demo.py init --schema ko_demo_review
python scripts/demo.py serve --schema ko_demo_review --port 8001
```

macOS/Linux 用 `export KNOWLEDGEOPS_DATABASE_URL='...'`。`--schema` 必须以 ko_demo 开头；不要选择真实业务 schema。`.env.example` 不会被此脚本自动加载。不要把真实密码写进 README 或实验报告。

## 容器运行

```sh
docker compose -f docker-compose.yml -f docker-compose.demo.yml up --build -d --wait
```

服务会安装依赖、等待数据库健康、初始化 Fake 样例，再启动 API。默认使用官方 Python 3.12 镜像。首次构建需要访问镜像与 Python 包源；如需更小镜像，可先单独 build --build-arg PYTHON_IMAGE=python:3.12-slim。打开 `http://127.0.0.1:8000/docs`。查看日志与停止：

```sh
docker compose -f docker-compose.yml -f docker-compose.demo.yml logs app
docker compose -f docker-compose.yml -f docker-compose.demo.yml down
```

不要加 `-v`，除非确实要删除数据库数据卷。容器配置用于本地演示，不代表生产部署已经完成。

### 包源访问失败
+
+容器默认使用 PyPI；本机验证中默认源返回 idna 依赖不可用，使用 Aliyun 镜像完成了同一锁文件的安装。可以显式选择包源，不需要修改全局 pip/Docker 设置：
+
+```sh
+python -m pip install -r requirements.lock --index-url https://mirrors.aliyun.com/pypi/simple/
+docker compose -f docker-compose.yml -f docker-compose.demo.yml build --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ app
+docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --wait
+```
+
+如果已有完整依赖镜像，不必再 build。GitHub Actions 可以通过仓库变量 PIP_INDEX_URL 选择包源；首次远端运行仍需查看实际安装与测试结果。
+
+## 测试与评测

```sh
python scripts/demo.py smoke
python scripts/demo.py test
python scripts/demo.py evaluate
python -m pip check
```

smoke 和 test 会生成独立随机 schema，结束只清理该次创建的 schema。完整测试还会生成随机租户、collection，并清理各自数据。标准测试不是 Fake 数据库：模型是 Fake，存储是真实 PostgreSQL。仅执行 `pytest` 且未设置数据库测试变量会跳过部分测试，所以推荐上述 test 命令。

smoke 通过 FastAPI TestClient 测试真实路由、服务和数据库；不是外部网络负载测试。报告位于 `data/demo-smoke.json`。评测写入 `docs/experiments/`，数据库快照复制到 ko_eval_<uuid>，这些原始产物默认不上传。管理员可按 manifest 的 snapshot_schema 删除对应评测 schema，不要批量删除业务 schema。

GitHub Actions 配置执行同样的完整测试，并校验架构页面。workflow 在实际远端运行前，只能称“已配置”，不能当作绿色 CI 成绩。

## 真实模型模式

先完成 Fake 复现。真实模式直接配置进程环境变量后运行 `python -m uvicorn app.main:app`，不要用固定 Fake 的 demo.py serve。三个模型模块独立配置：

| 模块 | 必要变量 |
| --- | --- |
| Embedding | KNOWLEDGEOPS_EMBEDDING_PROVIDER=compatible、MODEL_ID、DIMENSIONS、BASE_URL、API_KEY、COLLECTION |
| 问答生成 | KNOWLEDGEOPS_GENERATION_PROVIDER=compatible、MODEL_ID、BASE_URL、API_KEY |
| Agent | KNOWLEDGEOPS_AGENT_PROVIDER=compatible、MODEL_ID、BASE_URL、API_KEY、INPUT_PRICE、OUTPUT_PRICE |

前缀分别是 KNOWLEDGEOPS_EMBEDDING_ / GENERATION_ / AGENT_；完整选项见根目录 `.env.example`。统一写连接是 KNOWLEDGEOPS_DATABASE_URL；只读业务连接指向相同数据库、对应 schema 的 SELECT-only 账号。模型价格应填写实际账单口径，Embedding 费用预算也需相应配置。更换 Embedding 模型、维度或输入格式必须使用新 collection 并重新索引。真实调用可能计费，默认复现流程不调用它们。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| Docker 连接失败 | 先启动 Docker，执行 docker compose ps，确认数据库 healthy |
| 55432 / 8000 被占用 | 调整 Compose 的本机映射；API 使用 --port 8001 |
| serve 提示先初始化 | 在相同数据库与 --schema 下运行 init |
| 只读业务工具报权限错误 | 检查 init 是否成功创建 reader，writer / reader 是否指向相同 schema |
| 上传返回 202 没有正文 | 等待任务 succeeded；上传接收和解析完成不是同一个状态 |
| 问答没有预期答案 | Fake 是简单规则模型；先查 /retrieve 的证据，不把它当真实模型能力 |
| localhost 请求超时且服务没有访问日志 | 检查终端 HTTP_PROXY / HTTPS_PROXY；本机客户端设置 NO_PROXY=127.0.0.1,localhost，Python 测试客户端可用 trust_env=False |
| 中文输出乱码 | 使用 UTF-8 终端；PowerShell 旧版可优先用 Swagger 或 Python 客户端 |
| 旧 SQLite 环境变量报错 | 直接启动 API 时清除 KNOWLEDGEOPS_INGESTION_DB_PATH；demo 命令会隔离旧配置 |

`GET /health` 仅表示应用存活，不能替代持续的数据库依赖就绪检查。服务启动会检查/初始化数据库；完整数据库可用性由集成测试和实际请求验证。
