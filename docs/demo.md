# 五分钟 API 演示

文档更新、历史回滚、发布前后同题与权限对照的完整步骤见 [文档生命周期演示](document-lifecycle.md)。

先按 README 启动应用、配置真实模型，并上传文档构建索引或恢复样例备份，再打开 `http://127.0.0.1:8000/docs`。各业务请求填写请求头 `X-Local-User-Id: u_engineer_001`。接口返回的 ID 每次环境可能不同，以下使用实际响应值，不需要手工访问数据库。

## 1. 查证据，再问答

`POST /retrieve`：

```json
{"query":"TL-403 设备合规","strategy":"hybrid","candidate_k":20,"top_k":5}
```

展示 hits 中的文档来源、版本、位置、chunk_id 与 citation.url。`POST /chat`：

```json
{"message":"TL-403 设备合规刷新需要多久？","top_k":5}
```

展示 answer、claims、citations 和 conversation_id；用 `GET /citations/{chunk_id}?chunk_set_id=...` 读取对应证据。真实模型生成回答，具体是否回答取决于授权证据，不应把预期自然语言句子作为固定结果。

## 2. 看完整上传流程

`POST /documents` 上传小型 Markdown/TXT，记录 document_id、task_id；查询 `GET /ingestion/tasks/{task_id}` 直到 succeeded。之后依次：

1. `POST /documents/{document_id}/chunks`，请求体 `{}`。
2. `POST /documents/{document_id}/index`。
3. `POST /retrieve`，filters.document_ids 只填该文档 ID。

再次上传相同字节文件，应返回同一 document_id/task_id 和 duplicate=true。不同用户重复上传不能自动取得权限。这个操作会增加 demo 数据；如要运行严格的 14 份文档评测，使用独立数据库 schema 准备另一套样例。

## Agent 演示准备

仅使用问答无需配置业务演示。使用下面的业务查询和工单接口前，在选定的数据库与 schema 中运行 `scripts/setup_agent_demo.py`，准备虚构业务数据、工具权限和 SELECT-only 业务账号：

```sh
python scripts/setup_agent_demo.py --database-url "你的目标 PostgreSQL DSN"
```

该命令只用于演示环境，已有权限会保留。配置 `KNOWLEDGEOPS_DATABASE_URL` 和 `KNOWLEDGEOPS_AGENT_BUSINESS_DATABASE_URL` 时，两者须访问相应数据所在的 schema；后者使用脚本创建的 `knowledgeops_agent_reader` 只读账号。详细环境变量见仓库的 `.env.example`。工作台的聊天模型需填写真实输入、输出单价并保存，才能启用 Agent；知识库检索工具的 Embedding 费用预算另外沿用服务端价格配置。备份不包含这些业务演示数据或账号。

## 3. Agent 调用只读工具

`POST /agent`：

```json
{"message":"查询设备","request_key":"demo-device-0001"}
```

记录 run_id，查询 `GET /agent/{run_id}`，直到 status 不再是 running。展示 trace 中的工具调用与只读业务结果。也可以使用新 request_key 提交 `计算 12 + 28`。同一用户、相同 request_key 和原始消息复用既有任务；改消息而复用该 key 会冲突。

## 4. 写操作等待审批

`POST /agent`：

```json
{"message":"创建工单","request_key":"demo-ticket-0001"}
```

查询运行状态直到 waiting_approval。复制 pending 的 action_id、version、digest，查看 arguments 后提交 `POST /agent/{run_id}/approval`：

```json
{"action_id":"从 pending 复制","version":1,"digest":"从 pending 复制的64位摘要","approve":true}
```

这里的 approve=true 是申请人确认提交提案，不是业务审核通过。继续查询至 completed，展示 committed 工具结果中的 ticket_id。普通请求进入受理队列；敏感请求还需具备独立审核能力的身份在工单页处理，禁止自审，业务批准不等于已执行。再次提交同一个审批不会创建第二张工单。approve=false 会拒绝提案；每个演示新任务使用新 request_key。工单只存在于本地模拟表，不会发送邮件或创建外部工单。

## 5. 权限与事件回放

对工程师独有的样例或自己上传的文档，用 u_employee_001 的身份检索相同 document_id；应该返回空候选，引用访问应不可见。文档 ID 本身不授予权限。

`GET /agent/{run_id}/events?after=0` 是 SSE（Server-Sent Events，服务器发送事件）。客户端可以带 Last-Event-ID 继续读取历史事件。它回放任务状态和已校验的答案文本，不等于模型 Token 流式输出。

工程回归使用 `python scripts/run_tests.py`，它使用独立临时 schema，不调用付费模型。真实模型效果通过工作台导入标注测试集进行评测。
