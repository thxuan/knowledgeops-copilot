# 架构与设计取舍

## 业务目标

虚构企业 IT 文档分散、版本持续变化且有权限差异。RAG（Retrieval-Augmented Generation，检索增强生成）先找本轮可访问证据，再回答与引用；Agent 根据目标选择工具，查询当前业务状态或提出工单。RAG 本身能组织回答，Agent 的价值在多步骤工具执行和受控动作。

## 三条主流程

| 流程 | 执行顺序 | 关键入口 |
| --- | --- | --- |
| 文档 | 上传→去重→原始字节落库→worker 解析清洗→显式切块→Embedding→索引发布 | app/ingestion/service.py、worker.py、app/db/chunks.py、app/retrieval/service.py |
| 问答 | 服务端身份→当前可见集合→向量/BM25→RRF→可选 Rerank→上下文→真实模型生成 claims→引用与授权复核→会话提交 | app/retrieval/pipeline.py、app/generation/service.py、app/db/conversations.py |
| Agent | 创建幂等任务→租约认领→create_agent 标准模型/工具循环→middleware 确认暂停→事务提交→Command 恢复→最终回答 | app/agent/service.py、app/agent/middleware.py、app/db/agent_checkpoints.py、app/db/agent.py |

## 统一会话与模型调用

工作台自动模式先用真实聊天模型识别意图，知识问题进入 RAG，业务查询、计算与工单请求进入持久化 Agent。“仅问答”模式和批量评测直接进入 RAG。正常问答在检索后调用聊天模型生成答案；Agent 调用模型规划工具、接收执行结果并组织最终输出。没有可用证据时，问答可以在生成前拒答。

文档和问题向量化由 Embedding 模型完成；BM25、RRF、权限检查与事务提交由程序完成，可选 Rerank 使用单独的重排适配器。模型单价用于 Agent 的费用预算校验，不决定供应商是否对问答请求收费。

## 检索与证据选择

检索通道可分别控制语义与关键词召回，双路开启时使用 RRF 融合，之后按配置重排。候选数、融合保留数、证据窗口、上下文预算与生成采样参数均是不同的控制项。

| retrieval_profile | 当前行为 |
| --- | --- |
| `ranked` | 默认单查询排序；具体证据窗口由请求或工作台预设决定 |
| `coverage` | 单查询召回后，在同一候选池中去重，并兼顾查询词覆盖与来源多样性选择证据 |
| `multi_query` | 真实模型生成有界的互补子查询，各查询沿用身份和过滤器，召回结果融合后选择覆盖证据 |

查询规划非法、超时或部分补充检索失败时可退回原查询；规划不会授予权限。来源多样性不代表语义完整性，运行时不读取测试集标准答案来选择证据。

上下文使用短引用编号映射到真实切块，按完整片段处理预算并记录遗漏。生成自然语言答案，可配置用同一批证据复核草稿；复核与格式修复共享有界调用次数。只展示经过适用条件与引用校验的结论，不再将结论展开为整段来源，以免重新引入未确认条件下的操作。引用属于授权集合、可以定位，不等于每句事实都被来源支持，最终语义仍需评审。

## 存储与一致性

默认使用一个 PostgreSQL 数据库，pgvector 是安装在其中的向量扩展，不是第二个数据库。documents、document_access、ingestion_jobs/events 保存文档事实与 ACL；chunk_sets/chunks/document_chunk_heads 保存完整切块集合与当前指针；conversations/messages 保存用户会话。ko_* 向量表管理模型签名与发布快照，ko_agent_* 管理任务、审批动作、工单和模拟业务。

每次 Embedding 网络请求都在事务外完成，全部结果准备好后再事务发布向量头。文档 revision 与 chunk_set_id 防止旧内容成为有效证据；检索后、生成后分别复核授权。文档更新立即使旧头失效，新索引准备期间暂时不可检索。统一数据库不代表所有阶段使用一个长事务。

## 并发、恢复与幂等

- 上传依靠租户/文件哈希/解析器版本唯一键与事务级 advisory lock，重复上传不恢复 ACL。
- 摄取任务用 FOR UPDATE SKIP LOCKED 认领，claim_token 防止旧工作者覆盖新结果。
- 会话锁定目标会话行，并比较 revision；证据关联行持锁到提交，避免校验后撤销权限的竞态。
- Agent request_key 在用户和租户内去重，request_hash 防止同 key 改参数。租约和 token 控制同任务执行权，检查点用于恢复。
- 审批绑定 action_id/version/digest，提交时重新检查权限；工单唯一 action_id 与事务保证重复审批只产生一次本地效果。

## 技术选择

| 选择 | 原因与代价 |
| --- | --- |
| RAG | 文档持续更新且需引用；召回不足时生成无法补齐真实事实 |
| 混合检索 | 向量补语义，BM25 补编号与术语；RRF 融合排名而非直接加不同量纲的分数 |
| exact / HNSW | 精确检索用于基线；近似索引取舍速度与召回，小数据结果不代表大规模性能 |
| 单 Agent、LangChain create_agent | 框架提供模型/工具循环、工具分派和状态累积；middleware 衔接预算、结果校验与确认，业务库负责授权和工单事务。LangGraph 检查点支持持久恢复 |
| PostgreSQL（含 pgvector 扩展） | 文档、权限、会话、向量与业务状态集中管理；仍需明确事务边界和数据库角色 |
| 真实适配器 + 测试替身 | 运行时使用真实模型适配器；替身仅存在于 tests 中验证控制流，不能推断真实质量 |

## 安全边界

授权来自服务端身份映射，不采信模型参数里的身份或文档声明的 audience。业务查询仅接受固定 query 枚举，参数化 SQL，并用 SELECT-only 账号与只读事务。模型只能提出 create_ticket，真实提交来自独立审批 API。检索内容作为不可信数据处理；关键词注入检测只能告警，不能保证阻断所有攻击。

当前身份头只是本地测试映射，生产认证、部署密钥管理、监控告警和备份恢复仍需完善。不要把演示直接作为面向公网的生产服务。

参数和配置方法见 [工作台说明](workbench.md)，当前测试与质量指标口径见 [验证说明](verification.md)。历史试验和早期架构材料保存在本地配套的 knowledgeops-architecture 学习记录中。
