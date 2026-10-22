# 账号登录、后台索引与工单闭环

2026-10-18 第二批实现。没有企业身份服务也可以使用本项目自己的账号登录。当前默认仍为本机演示模式，避免升级后出现没有管理员密码而无法进入的情况。账号模式没有默认密码，不会把请求头身份切换当成登录。

## 1. 在本机启用账号模式

先安装更新后的 requirements.lock，启动现有 PostgreSQL。停止占用 8000 端口的旧工作台后，在项目根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_accounts.ps1
```

首次会在本机终端隐藏输入并确认管理员密码（12–256 字符）；重复运行复用已有启用管理员，不修改密码。脚本仅为 `127.0.0.1:8000` 本机 HTTP 验收启用非 Secure cookie。登录页面是 `/login`，管理员用户名默认 admin。首次管理员不允许从匿名网页创建，也不在聊天、命令行参数或日志中传递密码。

也可分步初始化，适合已有启动方式或自定义数据库：

```powershell
python scripts/manage_accounts.py --username admin
$env:KNOWLEDGEOPS_AUTH_MODE='accounts'
$env:KNOWLEDGEOPS_AUTH_DATABASE_URL='你的固定账号库 DSN'
$env:KNOWLEDGEOPS_PUBLIC_ORIGIN='http://127.0.0.1:8000'
$env:KNOWLEDGEOPS_AUTH_COOKIE_SECURE='false'
python scripts/workbench.py
```

初始化和运行必须使用同一账号库 DSN；账号库可以与知识库同库，但切换知识库连接不会移动账号。样例正文可能位于 ko_samples，启动脚本会把账号库独立固定，避免切换空间后丢失管理员。密码遗忘由服务主机运维者运行 `python scripts/manage_accounts.py --username admin --reset-password`；这会撤销该账号全部会话。脚本不会自动启用已停用账号。

正式部署须自行配置 HTTPS 与固定 PUBLIC_ORIGIN，使用 `KNOWLEDGEOPS_APP_ENV=production`、accounts 模式和 `KNOWLEDGEOPS_AUTH_COOKIE_SECURE=true`；启动校验拒绝生产环境的演示认证或非 Secure cookie。该开关只约束认证边界，不代表监控、迁移、备份或压力验收已完成。

## 2. 账号与管理权限

管理员在 `/admin/accounts` 创建、启用、停用账号。员工、工程师、IT 支持与平台管理员是首版固定角色模板；模板不授予文章阅读 ACL。平台管理员可以管理连接、模型和账号，并审阅当前组织与知识空间内的全部文档、草稿和历史，审批版本发布；平台管理权限不自动授予工单业务审批能力。IT 支持具有人工分诊、对应审批和 handle 工单处理能力，不拥有平台配置权限。

关联既有演示数据时，可由管理员显式填写对应 user_id，例如 u_engineer_001；新真实账号默认使用新生成 ID。只有在确认该账号确实应接管相应资料时才复用演示 ID。现存账号（含停用账号）的标识不能重复占用。权限页使用当前租户的启用账号目录，新增账号可作为发布审批的阅读对象，审批通过后可读当前正文，仍不能因此读取历史原文。

账号模式采用 [argon2-cffi PasswordHasher](https://argon2-cffi.readthedocs.io/en/stable/api.html) 保存 Argon2id 哈希；数据库只存会话随机值的摘要。Cookie 为 HttpOnly、SameSite=Lax，正式部署为 Secure；写请求同时检查会话 CSRF token 和 Origin。登录尝试有持久 IP 计数与账号暂时锁定。登录会话绝对有效期默认 8 小时，可通过 AUTH_SESSION_SECONDS 设置为 300–86400 秒。退出和账号停用删除会话，之后的请求立即失效；后台索引也会复核账号状态。

本机演示模式保留四个可切换身份，并显著显示“本地演示”。其中“平台管理员”用于演示连接配置，与工程师和 IT 支持分开。演示模式仍不应暴露到生产网络。历史/文章、工单业务审批、处理和平台管理是不同权限维度，不是身份等级高低。

## 3. 后台索引任务中心

知识库单文档、批量构建与草稿构建现在提交持久任务，返回 202 和 job_id，不再等待 Embedding 完成。打开 `/workbench/index-jobs` 查看本人任务、处理阶段、切块进度、尝试次数、错误原因，取消或重试。页面关闭不终止已提交任务。批量请求返回已接受项和每项错误，不把部分失败伪装成全部成功。

任务由服务 lifespan 中独立于 HTTP 请求的 IndexWorker 消费；不是 FastAPI 临时 BackgroundTasks。运行任务保存在 PostgreSQL，使用 SKIP LOCKED、120 秒租约、心跳和 fencing token；过期任务在服务恢复后可重新认领，最多 3 次。主动取消使旧回调无法写入索引，停用账号会阻断其任务提交。重领从任务起点重新处理，尚未实现分批向量断点缓存，因此异常重试可能重复产生模型费用。

模型签名与集合在提交时固定，不保存密钥；重启后未恢复相同 Embedding 时显示 blocked，不会偷偷使用另一模型。恢复配置后显式重试会使用当前模型和集合。准备中的草稿不参与检索；成功只表示索引就绪，仍需在知识库手动发布。当前正文任务成功时事务切换切块与索引，失败保留旧头。排队/运行任务存在时禁止切换数据库/Embedding 连接，先完成或取消；blocked 任务允许恢复配置，避免把任务遗落在其他空间。

服务内 worker 默认启用，INDEX_WORKER_ENABLED=false 可供测试/运维暂停消费；也可设置 Web 服务 INDEX_WORKER_ENABLED=false，在相同数据库与 Embedding 环境变量下单独运行 `python scripts/index_worker.py`。独立进程不读取 Web 内存中的密钥；模型、集合与账号模式配置须一致。暂无多租户配额或任务归档中心。索引队列、准备产物和原文历史纳入核心评测快照；账号凭据和会话不会被当成评测数据导出。旧同步 build/index API 为兼容保留，页面统一使用后台任务 API。

| API | 用途 |
| --- | --- |
| POST /index-jobs | document_id、可选 version_id、切块参数、force；提交或复用相同活动任务 |
| POST /index-jobs/batch | document_ids、切块参数、force；逐项接受并报告错误 |
| GET /index-jobs | 本人的最近任务，默认 50 项，最多 100 |
| GET /index-jobs/{id} | 本人任务详情，跨用户不可见 |
| POST /index-jobs/{id}/cancel | 取消排队、运行或配置阻塞任务 |
| POST /index-jobs/{id}/retry | 失败/取消/阻塞任务显式重试，最多 3 次尝试 |

## 4. 工单处理闭环与站内通知

普通工单：申请人确认提交 → 待受理 → IT 支持领取 → 处理中 → 处理人记录解决结果 → 待申请人确认 → 申请人关闭。申请人可对已解决/关闭工单写明原因重开，恢复待受理。

敏感工单须先由非申请人且具备完整对应能力的审核人审批，再允许领取。处理中发现敏感操作可升级审批并清空当前处理人；敏感工单重开后重新进入审批，不沿用旧批准直接处理。禁止自审，不允许普通申请人记录处理解决，也不允许处理人代申请人确认关闭。

双方可添加处理留言，记录操作者、时间、原状态、新状态和版本。所有活动都使用工单版本检查，并发领取/关闭只能有一个有效结果。业务状态页和只读工具从受限视图读取实际处理人，不再永远显示未分配；预计完成时间仍未实现，不填假值。

审批、领取、留言、解决、关闭和重开会向另一参与方写入持久站内通知。工单页可查看本人最近 50 条通知并标为已读，通知链接自动区分本人和处理队列。当前没有邮件、短信、附件、SLA、转派或外部工单连接器。解决结果是人工记录；系统不会据此宣称设备、权限或生产变更已经自动执行。

新增 API：POST /tickets/{id}/activity（action=claim/comment/resolve/close/reopen，version、reason），GET /notifications，POST /notifications/{id}/read。账号登录 API 见 /docs 的 /auth 命名空间；创建、停用账号及连接配置均需平台管理员。

## 5. 验证与仍待完成的生产工作

新增真实 PostgreSQL 集成回归覆盖账号登录/CSRF/伪造身份/停用/限流/动态 ACL，队列持久化、取消、旧租约、模型变化及恢复，工单领取、并发、敏感审批、留言、解决、申请人确认、重开和通知隔离。测试模型替身仅在 tests 和专用 UI 验收进程使用，日常运行仍要求真实模型。

浏览器使用隔离 schema 和一次性虚构账号验证管理员目录、工程师管理隔离、草稿离页后完成、支持领取到申请人关闭、站内通知及窄屏布局。不把该演示当作真实企业身份服务或模型语义质量验收。

尚需：企业 OIDC/MFA、自助账号恢复与更完整的角色/组织权限、配置和密钥持久化、正式数据库迁移、集中监控与告警、全业务备份恢复演练、容量与真实模型质量验收、实际工单系统对接。没有这些证据时不宣称生产 SLA 或全面生产就绪。
