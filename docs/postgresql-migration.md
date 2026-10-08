# SQLite 到 PostgreSQL 的一次性迁移

当前应用只使用 PostgreSQL。新环境不需要旧 SQLite 文件，直接按 [复现指南](reproduce.md) 初始化样例即可。本页仅面向持有本地旧版本数据的使用者。

停止旧应用写入后，设置 KNOWLEDGEOPS_DATABASE_URL 指向目标 PostgreSQL，并执行：

```sh
python scripts/migrate_sqlite_to_postgres.py --source path/to/ingestion.sqlite3
```

目标核心表必须为空；已有向量与 Agent 表不会被覆盖。导入保留文档、任务、切块、当前集合头、ACL、版本、事件、会话及消息 ID，恢复自增序列；缺少的 active/revision/updated_at 使用兼容默认值。权限不会在导入时额外授予。源文件只读且不删除；目标导入在一个事务内完成，失败整批回滚。

多个历史实验库可能包含同一文档的不同配置，不能直接合并到一个数据集；使用明确隔离的 schema / 数据库逐个迁移。导入报告默认写入 data/postgresql-migration.json，包含计数与源文件摘要，不包含连接密码。

事务、当前头与权限设计见 [设计取舍](design.md)，验证范围见 [验证记录](verification.md)。
