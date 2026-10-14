# 启动与复现

复现需要先准备 Git、Python 3.11+（建议 3.12）和正在运行的 Docker Compose 环境。Windows 需要启动 Docker Desktop 并使用 Linux 容器。克隆命令、虚拟环境安装与页面操作见 [快速开始](../README.md#快速开始)。克隆仓库不会带入维护者本地的连接配置和 API Key。

从项目根目录安装 `requirements.lock`，执行 `docker compose up -d --wait vectors`，再执行 `python scripts/workbench.py`。访问 http://127.0.0.1:8000/settings 配置真实模型和数据库。已有 `ko_samples` 文档库会在未显式配置数据库时优先使用；不会自动生成模拟向量或重新写入样例权限。

`.env.example` 仅为配置参考，不自动加载。`unconfigured` 表示尚未配置模型；通过环境变量启动真实适配器时使用 `compatible`，并设置对应 Key、Base URL、模型名称及向量维度。修改 Embedding 模型或维度时使用新的索引集合并重新构建。

## 容器运行

```sh
docker compose -f docker-compose.yml -f docker-compose.demo.yml up --build -d --wait
```

容器直接启动应用，不初始化 Fake 数据。页面连接设置里的数据库地址需使用容器可访问的主机名，不能把容器的 `127.0.0.1` 当作宿主机。内置数据库凭据仅用于本地演示。

## 验证

```sh
python scripts/run_tests.py -q
node --test tests/unit/*.mjs
```

测试入口在独立随机 schema 中准备数据库集成测试和只读业务账号，完成后只删除该临时 schema。数据库账号需有创建 schema、扩展和本地测试角色的权限。可通过 `KNOWLEDGEOPS_TEST_DATABASE_URL` 指定测试数据库。离线测试不调用付费模型。

## 包源访问失败

可为本次安装显式指定 `pip --index-url`，无需修改全局配置。查看容器日志使用 `docker compose -f docker-compose.yml -f docker-compose.demo.yml logs app`。停止服务使用同一 Compose 配置的 `down`；保留数据卷即可保留文档和索引。


## 使用预计算样例数据库

首次克隆可按 [样例备份说明](../sample_data/backups/README.md) 恢复 `ko_samples`，再配置自己的生成模型与 Embedding Key，直接运行配套 JSONL 评测。恢复工具会拒绝已有同名 schema，并校验文档、权限、切块和真实向量；不覆盖本地现有知识库。

离线回归验证工程功能，不需要真实模型 Key。在线问答与批量评测需要可用的生成模型；语义检索还需要与索引一致的 Embedding 服务。使用其他 Embedding 模型时，上传样例或自己的文档，在新集合中重新构建索引。Agent 演示还需按 [API 演示](demo.md) 准备业务数据与只读账号。
