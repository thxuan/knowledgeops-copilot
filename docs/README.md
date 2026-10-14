# 文档目录

首次使用按仓库首页的 [图文快速开始](../README.md#快速开始) 操作。这里保留当前使用、部署、实现和验证资料。

| 文档 | 用途 |
| --- | --- |
| [工作台说明](workbench.md) | 连接设置、统一会话、运行参数、测试集、批量上传与索引、浏览器缓存 |
| [启动与复现](reproduce.md) | 本机和容器运行、环境变量、离线回归与样例备份 |
| [架构与设计取舍](design.md) | 模型调用、单库存储、检索策略、事务、权限与执行边界 |
| [API 演示](demo.md) | 接口调用、文档流程、Agent 业务准备、只读工具与人工审批 |
| [验证证据与能力边界](verification.md) | 当前工程验证、备份恢复验证、指标口径和尚未验证的范围 |
| [旧 SQLite 数据导入](maintenance/postgresql-migration.md) | 可选维护步骤；首次安装不需要 |
| [样例数据库备份](../sample_data/backups/README.md) | ko_samples 恢复条件、模型兼容要求与恢复命令 |

`images/quickstart/` 保存首页使用的操作截图。`evidence/` 只保存当前保留功能的工程验证汇总；这些文件不会作为模型输入，也不是最终答案准确率成绩。

Day 1 需求基线、旧发布准备、跨文档修复与检索试验，以及历史验证记录已归档到本地配套项目 `knowledgeops-architecture/learning-records/copilot/docs-history-20261014/`，附原始提交与 SHA-256。主仓库不再把这些旧记录列为当前操作指南。
