# ko_samples 样例知识库备份

[ko_samples.dump](ko_samples.dump) 是可恢复的 PostgreSQL custom-format 备份（约 581 KB）。[ko_samples.json](ko_samples.json) 记录 SHA-256、模型签名、表行数与样例文件哈希。恢复校验容许 Git 在 Windows 上把 Markdown 的 LF 换成 CRLF，仍严格核对数据库原始字节哈希、正文、元数据、权限和切块。备份从本地 `ko_samples` 的临时副本导出，保留 14 份虚构文档、38 条演示权限、100 个当前切块和 100 个真实 Embedding 向量。恢复无需重新批量 Embedding，也不会调用模型。

## 使用条件

| 项目 | 要求 |
| --- | --- |
| 数据库 | PostgreSQL 17；推荐仓库的 Docker Compose |
| 向量扩展 | pgvector 0.8.0；Compose 镜像已安装，恢复脚本自动启用扩展 |
| Python | 3.11+，已安装项目的 `requirements.lock` |
| 恢复工具 | Docker Compose；脚本调用容器内的 `pg_restore` 和 `psql`，无需主机安装 PostgreSQL 客户端 |
| 目标位置 | Compose 的 `knowledgeops` 数据库；必须尚无 `ko_samples` schema |
| Embedding | 阿里云百炼北京区域的 `text-embedding-v4`，1024 维 |
| 向量集合 | `bailian-v4-1024-validation-v1` |
| 距离/文本格式 | cosine / `title-heading-text-v1`，与项目当前适配器一致 |
| 在线问答 | 使用者自己的生成模型与 API Key；语义检索还需要自己的北京区域 Embedding Key |

供应商的北京区域 Base URL 格式为 `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`，请替换为自己的工作空间。模型与维度说明见[阿里云官方 Embedding API 文档](https://www.alibabacloud.com/help/zh/model-studio/text-embedding-synchronous-api)。

备份中的 endpoint 已替换为不可用占位地址，不包含导出者的工作空间地址。恢复参数只把这个占位签名绑定到使用者自己的北京区域服务地址，模型、维度与向量数据均保持原值。此操作仅适用于同一供应商、区域和同名模型的工作空间迁移；不能用签名替换来兼容其他模型、维度、私有代理模型或供应商变更后的向量空间。需要更换时请创建新集合并重新计算向量。

## 快速恢复

从仓库根目录执行。先安装依赖并启动数据库，再恢复，最后启动应用：

```sh
python -m pip install -r requirements.lock
docker compose up -d --wait vectors
python scripts/sample_backup.py restore --embedding-base-url "https://YOUR_WORKSPACE_ID.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
python scripts/workbench.py
```

将 `YOUR_WORKSPACE_ID` 换为自己的北京区域工作空间 ID，Base URL 不带 `/embeddings`，也不包含 API Key。恢复脚本先核对备份 SHA-256，在一个事务内启用 pgvector、恢复 schema 与数据、绑定地址，再检查文档、切块、权限和向量头的一致性。

打开 `http://127.0.0.1:8000/settings`：

1. 数据库使用默认主机 `127.0.0.1`、端口 `55432`、库名 `knowledgeops`、用户 `knowledgeops`、开发密码 `local-day5-only`；schema 选择 `ko_samples`，点击检测连接。
2. 选择集合 `bailian-v4-1024-validation-v1`，确认模型为 `text-embedding-v4`、维度为 `1024`、Base URL 为恢复时填写的地址；输入自己的北京区域 Key，测试并应用连接。
3. 配置自己的生成模型 Base URL、模型名称和 Key。Rerank 可选。
4. 知识库已包含索引，直接进入“会话与评测”，导入 `eval_data/questions.jsonl`，需要章节指标时同时导入 `eval_data/day4-source-spans.jsonl`，选择 `dev` 后开始评测。

工程师和 IT 支持各可见 13 份文档，普通员工可见 12 份；这是样例权限规则。评测题目也按身份过滤，不能把不可见文档的题目与其他身份成绩直接混算。来源与章节指标需要标注；答案正确性仍需人工评审。生成模型可以独立更换，不影响预计算的 Embedding。

**模型调用条件：** 恢复、列表与离线验证不调用模型。在线语义检索仍需为每条问题调用 Embedding，答案生成需要调用聊天模型，因此仍可能产生供应商费用；预计算向量只省去文档的批量 Embedding。此备份不包含 API Key。

## 已有本地知识库

如果 `ko_samples` 已存在，脚本会拒绝恢复，不删除、不覆盖，也不会改动原集合签名。你现在已有的本地知识库不需要恢复。可以选择另一个新建数据库测试：

```sh
docker compose exec -T vectors createdb -U knowledgeops knowledgeops_samples_demo
python scripts/sample_backup.py restore --database knowledgeops_samples_demo --embedding-base-url "https://YOUR_WORKSPACE_ID.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
```

该数据库仍在同一个 Compose 服务上；启动应用后在连接设置将数据库切换为 `knowledgeops_samples_demo`、schema 选择 `ko_samples`。如果没有显式配置数据库，启动脚本只自动发现默认 `knowledgeops` 库中的 `ko_samples`。恢复脚本只支持本仓库 Compose 的本地 PostgreSQL 地址，不用于任意远程数据库。

## 备份范围与限制

只包含知识库的 11 张表；未包含历史会话、Agent 执行/审批/工单、解析事件、Embedding 调用记录、旧 Fake/未配置向量集合、数据库账号权限或原工作空间地址。应用启动时自动补齐空的会话与运行表。

真实集合原本使用精确向量检索，备份未携带旧测试集合的 HNSW 索引。快速测试保持 `exact` 模式；使用 HNSW 前必须在目标集合显式建立对应索引。备份不提供 Agent 的业务演示数据或只读业务账号，这些仍由 `scripts/setup_agent_demo.py` 单独配置。

若供应商模型更新、维度或输入格式改变，已有向量可能不再兼容，需使用新集合重新索引。数据库备份是固定版本的测试起点，不代表最新模型质量，也不包含运行时配置。

维护者需要重新生成备份时，在样例库所在的 Compose 环境执行：

```sh
python scripts/sample_backup.py export
```

导出会验证原始样例内容、权限、解析结果和切块，再在独立临时数据库中保留真实集合并清除运行数据，最后导出；不会修改源 `ko_samples`。目标数据不再是原始 14 份样例时会拒绝导出。该维护命令需要创建/删除临时数据库的权限。

## 恢复验证（2026-10-14）

已在独立 PostgreSQL 17 + pgvector 0.8.0 数据库中实际恢复，核对全部文档与向量数；验证三种身份的访问范围、BM25 HTTP 检索、已存真实向量的最近邻检索、已有 schema 拒绝覆盖、校验和拒绝与 SQL 失败事务回滚。验证未调用外部模型，原本地知识库保持不变。完整汇总见[验证记录](../../docs/evidence/sample-backup-20261014.json)。
