# 架构与设计取舍

## 业务目标

虚构企业 IT 文档分散、版本持续变化且有权限差异。RAG（Retrieval-Augmented Generation，检索增强生成）先找本轮可访问证据，再回答与引用；Agent 根据目标选择工具，查询当前业务状态或提出工单。RAG 本身能组织回答，Agent 的价值在多步骤工具执行和受控动作。

## 三条主流程

| 流程 | 执行顺序 | 关键入口 |
| --- | --- | --- |
| 文档 | 上传→去重→原始字节落库→worker 解析清洗→显式切块→Embedding→索引发布 | app/ingestion/service.py、worker.py、app/db/chunks.py、app/retrieval/service.py |
| 问答 | 服务端身份→当前可见集合→向量/BM25→RRF→可选 Rerank→上下文→claims→引用与授权复核→会话提交 | app/retrieval/pipeline.py、app/generation/service.py、app/db/conversations.py |
| Agent | 创建幂等任务→租约认领→模型提出工具→Schema/授权校验→执行回填→审批或最终回答→检查点 | app/agent/service.py、app/db/agent.py |

## 存储与一致性

统一 PostgreSQL。documents、document_access、ingestion_jobs/events 保存文档事实与 ACL；chunk_sets/chunks/document_chunk_heads 保存完整切块集合与当前指针；conversations/messages 保存用户会话。ko_* 向量表管理模型签名与发布快照，ko_agent_* 管理任务、审批动作、工单和模拟业务。

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
| 单 Agent、自写循环 | 工具数量和任务范围有限，可直接控制协议、状态、预算、审批；复杂任务扩展需要新评测依据 |
| PostgreSQL + pgvector | 文档、权限、会话、向量与业务状态集中管理；仍需明确事务边界和数据库角色 |
| Fake + compatible 适配器 | Fake 保证便宜可复现的控制流验证；真实模型适配器验证实际接口，不能用 Fake 分数推断真实质量 |

## 安全边界

授权来自服务端身份映射，不采信模型参数里的身份或文档声明的 audience。业务查询仅接受固定 query 枚举，参数化 SQL，并用 SELECT-only 账号与只读事务。模型只能提出 create_ticket，真实提交来自独立审批 API。检索内容作为不可信数据处理；关键词注入检测只能告警，不能保证阻断所有攻击。

当前身份头只是本地测试映射，生产认证、部署密钥管理、监控告警和备份恢复仍需完善。不要把演示直接作为面向公网的生产服务。

详细代码入口见 [交互式架构导航](architecture/index.html)，ID、键和约束见 [字段与机制整理](architecture/identifiers-and-constraints.md)。
