# 文档目录

[整仓清理记录（2026-10-20）](repository-cleanup.md)：删除范围、保留边界与全量回归结果。

首次使用按仓库首页的 [图文快速开始](../README.md#快速开始) 操作。这里保留当前使用、部署、实现和验证资料。

| 文档 | 用途 |
| --- | --- |
| [LangChain 生态迁移](langchain-migration.md) | 封存、业务验收标准、分阶段替换与旧实现删除规则 |
| [Agent 图与第三阶段验收](agent-langgraph.md) | LangGraph 执行、持久检查点、确认恢复和用户验收步骤 |
| [账号与后台业务](accounts-and-operations.md) | 无外部身份服务的账号登录、管理员初始化、索引任务中心、工单闭环和站内通知 |
| [文档版本、发布与权限](document-lifecycle.md) | 草稿到发布、历史差异、回滚、撤回、实际 ACL 与可重复演示 |
| [完整性与生产试点路线](production-readiness-roadmap.md) | 文档更新、历史版本、权限矩阵、演示场景与生产落地缺口及分阶段验收 |
| [企业支持中文知识库](../datasets/enterprise-zh/README.md) | 真实中文终端指南、组织政策、独立导入和全部现有 Agent 场景映射 |
| [业务状态说明](business-status.md) | 设备、账号安全、本人已提交工单及独立耗时示例 |
| [工单体验与验收](ticket-experience.md) | 排障过程、可编辑提案、审批回执、工单列表与详情 |
| [2026-10-16 更新日志](../CHANGELOG.md#2026-10-16) | 中文知识库、业务与工单、流式回答、评审与界面更新 |
| [会话界面](chat-interface.md) | 布局、统一身份、引用来源与审批过期退出 |
| [工作台说明](workbench.md) | 连接设置、统一会话、运行参数、测试集、批量上传与索引、浏览器缓存 |
| [启动与复现](reproduce.md) | 本机和容器运行、环境变量、离线回归与样例备份 |
| [架构与设计取舍](design.md) | 模型调用、单库存储、检索策略、事务、权限与执行边界 |
| [API 演示](demo.md) | 接口调用、文档流程、Agent 业务准备、只读工具与人工审批 |
| [验证证据与能力边界](verification.md) | 当前工程验证、备份恢复验证、指标口径和尚未验证的范围 |
| [样例数据库备份](../sample_data/backups/README.md) | ko_samples 恢复条件、模型兼容要求与恢复命令 |

`images/quickstart/` 保存首页使用的操作截图。`evidence/` 只保存当前保留功能的工程验证汇总；这些文件不会作为模型输入，也不是最终答案准确率成绩。

Day 1 需求基线、旧发布准备、跨文档修复与检索试验，以及历史验证记录已归档到本地配套项目 `knowledgeops-architecture/learning-records/copilot/docs-history-20261014/`，附原始提交与 SHA-256。主仓库不再把这些旧记录列为当前操作指南。

- [文档审阅与发布审批](document-review.md)：管理员审阅、版本审批、共享范围与升级规则。
