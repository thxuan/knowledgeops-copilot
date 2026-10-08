# KnowledgeOps：ID、键、版本与约束对照

核对日期：2026-10-07。依据项目实际代码整理，用于理解与面试口述。

先区分三个层次：

1. 字段：描述对象，例如 document_id、action_id。
2. 校验条件：判断能否继续，例如当前身份有权限、审批未过期。
3. 执行机制：落实规则，例如事务、唯一约束、锁、租约与预算计数。

**知道对象的 ID，只能定位对象，不自动获得访问或执行权限。**

## 1. 身份：谁在访问？

| 字段／记录 | 标识什么 | 实际用途 | 它单独不能保证什么 |
| --- | --- | --- | --- |
| tenant_id | 数据属于哪个租户 | 文档、会话、Agent、业务查询中的租户过滤 | 不能替代用户权限 |
| user_id | 当前用户 | 文档 ACL、会话／Agent 归属、业务行过滤 | 客户端随意提供的 ID 不可信 |
| document_access(document_id, user_id) | 哪个用户能读哪份文档 | 文档与引用查询连接 ACL 表检查 | 同租户不意味着所有文档都能读 |
| uploaded_by | 文档上传者 | 更新、删除、修改权限时检查所有者 | 读权限不意味着可以改文档 |
| can_write | 当前用户是否有创建工单权限 | 审批提交事务内重新检查并锁定权限行 | 用户点击批准不自动授予写权限 |
| audiences | 身份上的受众标签 | 本地身份映射中保存的分组信息 | 当前主要文档读取防线是 document_access；不能只看到标签就认为已做完整分组授权 |

当前身份由 app/core/identity.py 中的服务端测试身份映射建立；它仍是本地演示认证。

## 2. 文档：哪份内容、哪次处理、哪套切分？

| 字段 | 标识什么 | 如何使用 |
| --- | --- | --- |
| document_id | 一份逻辑文档 | 上传、更新、切分、索引都通过它关联；更新正文保留 document_id |
| task_id | 这份文档的摄取任务 | 查询解析阶段、失败原因、重试；当前一份文档只有一条 ingestion_jobs，重试复用 task_id |
| file_hash | 原始文件字节的 SHA-256 指纹 | 与 tenant_id、parser_version 一起组成摄取去重唯一键 |
| parser_version | 解析器／解析配置版本 | 同字节文件在不同解析规则下不能直接视作同一处理结果 |
| version | 面向业务的文档版本标签 | 例如 v1、v2；更新时检查 expected_version 是否等于当前标签 |
| revision | 文档内部修订计数 | 更新／删除推进计数；切分提交前复核，发现处理期间文档改变则拒绝提交 |
| config_id | 切分配置标识 | 包含算法版本、strategy、size、overlap，说明按什么规则切分 |
| text_hash | 清洗后整份正文的 SHA-256 | 标识正文内容；当前 Chunk 中的 text_hash 是整份清洗正文哈希，不是单个片段哈希 |
| chunk_set_id | 某份文档的一整套切分结果 | 由文档、正文哈希、切分配置、解析版本、revision 等生成；用来对齐当前切分与索引 |
| chunk_id | 一套切分中的具体片段 | 检索结果、结论引用与原文读取定位到它；参数、位置或版本等改变时可变化 |
| collection | 一组固定配置的向量空间 | 绑定 provider、模型、维度、端点、距离与输入格式；换配置通常用新集合 |

关系例子（以下短编号仅为教学示意）：

```text
document_id = D1：VPN 指南
  ├─ task_id = J1：解析这份文档
  └─ 当前切分头 → chunk_set_id = S1
       ├─ chunk_id = C1：登录步骤
       └─ chunk_id = C2：TL-403 处理说明

collection = V1：固定的 Embedding 模型配置
  └─ D1 的向量头 → S1
       ├─ (V1, C1) 的向量
       └─ (V1, C2) 的向量
```

**当前头是一个指向有效集合的记录，不是另一个随机 ID。**

- PostgreSQL document_chunk_heads：D1 当前采用 S1。
- PostgreSQL ko_vector_heads：(V1, D1) 当前索引采用 S1。
- 检索同时匹配授权范围、当前切分集合与已发布向量头。
- 更新 D1 后保留 document_id，但删除旧切分头；重新切分得到 S2，再为 S2 建索引。旧内容即使物理保留，也不能继续作为当前证据返回。
- file_hash 不包含权限，也不是加密；重复上传复用文档时仍需校验现有 ACL。

## 3. 会话与诊断：在哪段对话、哪次访问、哪次实验？

| 字段 | 标识什么 | 重要区别 |
| --- | --- | --- |
| conversation_id | 一段 RAG 多轮会话 | 多次 /chat 可沿用同一会话；读取仍需校验租户与用户归属 |
| message_id | PostgreSQL 会话表中的一条消息 | 自增 ID，用于排序、分页、摘要回溯；ID 本身不包含消息内容 |
| conversations.revision | 会话的已提交版本 | 同一历史快照发起的两个回答，后提交者发现版本改变后返回冲突，避免乱序覆盖 |
| request_id | 一次 HTTP 请求 | 中间件每次新生成，出现在响应头和日志；重试、查询、审批通常各有不同 request_id |
| trace_id | Agent 保存的创建请求关联号 | 当前创建任务时保存该次 request_id，不是一个完整分布式追踪系统 |
| Embedding 的 run_id | 一次索引或向量检索操作 | 串联该次操作的批次日志，与 Agent run_id 是不同含义 |
| batch_id | 一次 Embedding 批次尝试的日志记录 | 同一逻辑批次重试会生成新 batch_id；用 batch_number 和 attempt 区分批次及次数 |
| ingestion_events.event_id | 一条摄取事件 | PostgreSQL 自增事件主键，关联 task_id |
| Agent events 中的 id | 某个 Agent 任务内的一条事件 | 在 run 内递增，配合 run_id 与 Last-Event-ID／after 补发；不是全局业务幂等键 |

**同名 run_id 需要结合模块理解。检索操作、Agent 任务、离线实验的 run_id 不是同一张记录。**

## 4. Agent：同一任务、工具调用、写动作和工单

| 字段 | 谁产生 | 作用 | 哪些操作沿用 |
| --- | --- | --- | --- |
| request_key | 客户端提交 | 表示“这是同一个业务请求”；唯一范围为 tenant_id + user_id + request_key | 相同请求重试沿用；新任务使用新键 |
| request_hash | 服务端计算 | 原始请求 message 的 SHA-256，检查旧 request_key 是否被绑定到不同内容 | 相同键但 message 改变返回冲突 |
| Agent run_id | 服务端创建 | 标识整项持久化任务，保存阶段、预算、消息、轨迹和结果 | 查询、恢复、审批、取消、事件订阅都使用原 run_id |
| tool_call_id | 模型调用协议返回 | 对上某个工具请求和相应结果；同一 run 内校验调用 ID 不重复 | 工具结果回填使用同一个 ID |
| call_id | 服务端动作／轨迹记录 | 保存上述 tool_call_id；是同一调用标识在内部记录中的字段名 | 创建提议和批准后的工具回填使用它 |
| action_id | 服务端创建提议 | 标识一次待审批写动作，作为工单副作用的幂等身份 | 重复批准相同动作复用它 |
| action.version | 服务端保存 | 待审批动作的版本；当前实现为 1，没有修改动作参数的接口 | 审批提交必须匹配保存值 |
| digest | 服务端计算 | SHA-256(run_id、工具名、规范化参数、动作版本)，检查审批绑定内容 | 审批必须提交匹配摘要；摘要不是用户权限凭据 |
| expires_at | 服务端保存 | 待审批动作的截止时间 | 超期的 pending 动作不能新提交 |
| ticket_id | 服务端成功写入时创建 | 最终业务工单的 ID | action_id 唯一，重复批准不会生成第二个 ticket_id |

示意：

```text
用户请求：创建 VPN 故障工单
  request_key = K1
    → run_id = R1：整个 Agent 任务
      → tool_call_id = T1：一次 create_ticket 调用
        → action_id = A1：保存的待审批动作
          version = 1、digest = H1、expires_at = 截止时间
          → 用户批准 + 当前 can_write 校验
            → ticket_id = B1：实际写入的一张工单
```

三个常见重试分别保护不同层次：

1. POST /agent 超时后，原 request_key + 原 message 复用 R1，避免新建任务。
2. 进程中断后，POST /agent/R1/resume 恢复原 R1，避免把恢复变成新业务请求。
3. 批准 A1 后响应丢失，重复批准 A1 返回已保存状态，避免再创建工单。

相同参数新建两个 task/action，不会自动被视为同一业务动作。幂等范围由原键及身份定义。

## 5. 执行权：当前谁有资格保存结果？

| 字段 | 所在模块 | 作用 |
| --- | --- | --- |
| claim_token | 文档摄取 | 每次领取生成新的 token；advance／complete／fail 需要匹配当前 token，接管后旧 worker 不能覆盖结果 |
| lease_token | Agent | 每次执行器领取生成新的 token；保存检查点和提议时校验 token、租约时间和运行状态 |
| lease_until | 两类任务都有 | 租约截止时间，未过期时保留执行权；过期后可以按模块规则重新领取 |

任务 ID 和租约 token 不能互相替代：

```text
同一个 run_id = R1
  执行器甲领取：lease_token = L1
  甲中断 → 租约过期
  执行器乙接管：lease_token = L2
  甲恢复并试图提交 L1 → 拒绝
```

Agent 当前需要显式 resume，摄取 worker 则会轮询并重领过期任务。租约过期不会自动杀进程，也不能撤销已经发生的外部副作用。摄取提交校验当前 token 与 processing 状态；不要误说它和 Agent 一样在所有提交位置都直接检查 lease_until。

## 6. 数据库与程序约束：哪些规则由谁落实？

| 机制 | 核心问题 | 当前例子 |
| --- | --- | --- |
| PRIMARY KEY（主键） | 怎样唯一定位一行？ | document_id、run_id、action_id、ticket_id |
| UNIQUE（唯一约束） | 哪个业务组合不能重复？ | (tenant_id,file_hash,parser_version) 不重复摄取；(tenant_id,user_id,request_key) 不重复建任务；tickets.action_id 不重复写工单 |
| FOREIGN KEY（外键） | 引用的记录是否存在？ | 工单 action_id 必须关联动作；向量关联 Chunk 和集合维度 |
| CHECK（检查约束） | 存储值是否合法？ | vector_dims 等于配置维度；向量非零 |
| Schema／Pydantic 参数校验 | 请求结构和取值是否符合协议？ | 工具名白名单、数值范围、查询枚举；不等于授权 |
| 授权查询与业务判断 | 当前身份能否读／执行？ | tenant_id + user_id + ACL；审批提交检查 can_write |
| 版本／摘要复核 | 我处理的还是刚才那一份内容吗？ | expected_version、revision、chunk_set_id、审批 version + digest |
| 行锁／事务级 advisory lock | 同时修改时怎样协调？ | FOR UPDATE 锁定 run 和 action；领取时 advisory lock 协调全局租约计数 |
| Transaction（事务） | 多个修改会不会只成功一半？ | 工单、动作结果、工具回填及任务状态同库提交或一起回滚 |
| Lease（租约） | 谁现在拥有执行权？ | token + lease_until + 状态校验；接管隔离旧执行器 |
| 并发计数／信号量 | 同时最多执行多少任务？ | Agent 活跃租约数与本进程 jobs；聊天 active；Embedding semaphore |
| 速率计数 | 一段时间能发起多少新任务？ | Agent 按租户用户统计最近一分钟新任务；聊天本进程 history |
| 预算／次数上限 | 一项任务最多消耗多少资源？ | max_steps、max_seconds、max_tokens、max_cost、max_attempts |

唯一约束防止重复键，不限制不同任务的总并发。事务保证原子提交，不自动保证同一业务动作不会再次提交。锁协调并发操作，不代表用户有权限。

## 7. 最容易混淆的配对

| 配对 | 一句话区分 |
| --- | --- |
| request_id / request_key | 一次网络请求的诊断号 / 一个业务请求的重试身份 |
| task_id / Agent run_id | 文档解析任务 / 智能体业务任务 |
| run_id / tool_call_id / action_id / ticket_id | 整个任务 / 一次工具调用 / 一次待审批写动作 / 最终工单 |
| document_id / chunk_set_id / chunk_id | 一份文档 / 一套切分 / 一个片段 |
| 对象 ID / lease_token | 要操作哪项任务 / 当前执行器有没有执行权 |
| 文件 hash / request_hash / 审批 digest | 上传内容指纹 / 请求内容绑定 / 审批动作内容绑定 |
| 文档 version / 文档 revision / 会话 revision / 动作 version | 业务标签 / 内部文档变化计数 / 会话提交变化计数 / 审批动作版本 |
| ID 存在 / 用户有权限 | 找得到记录 / 当前身份能否访问，必须分别判断 |

另外，api_key 是调用模型供应商服务的凭据；claim_token／lease_token 是任务执行权标识；模型 Token 是文本处理与计费单位。这三种东西用途不同，名称里的 key 或 token 不代表它们属于同一种机制。

## 8. 代码入口

- 身份映射：../knowledgeops-copilot/app/core/identity.py → get_identity。
- 上传去重与摄取租约：../knowledgeops-copilot/app/db/ingestion.py → submit、claim、advance、complete。
- Chunk 集合与当前头：../knowledgeops-copilot/app/db/chunks.py → build。
- 文档版本与引用读取：../knowledgeops-copilot/app/db/documents.py → update、citation。
- 向量集合约束：../knowledgeops-copilot/app/db/vectors.py → initialize、_check、publish、search。
- 会话归属与并发版本：../knowledgeops-copilot/app/db/conversations.py → _owned、commit。
- Agent 请求幂等、租约、动作与工单：../knowledgeops-copilot/app/db/agent.py → create、claim、checkpoint、propose、approve。
- 调用 ID 校验与结果回填：../knowledgeops-copilot/app/agent/service.py → execute。
- HTTP 请求 ID：../knowledgeops-copilot/app/main.py → request_context。
- 事件 ID：../knowledgeops-copilot/app/agent/contracts.py → event。

本文是字段与机制参考，没有把走读、用户理解题或面试练习标记为完成。
