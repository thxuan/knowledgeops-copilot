# 企业技术支持中文知识库

这套资料为原有企业技术支持 Agent 增加真实中文排障内容。技术资料来自 GNOME 官方简体中文帮助，适用 Linux/GNOME 办公终端；企业组织规则仍使用原有明确标注的虚构政策。设备状态、工单耗时和建单仍由现有 PostgreSQL 工具提供，不从文档推测。

资料准备快照包含 41 篇真实中文指南和 14 篇演示组织政策，共 55 篇；按导入脚本默认的 800 字符结构切分预览为 232 块。preparation.json 记录导入前阶段，当前空间的文档和向量状态以工作台为准。2026-10-16 已索引快照的来源、权限与题目见 [对应测试集](../../eval_data/enterprise-zh-20261016/README.md)。下载内容、数据库及向量索引属于运行数据，不包含在 Git 中。

## 资料组成与授权

- 真实技术指南：VPN、无线/有线网络、代理、账号权限、文件访问、备份、磁盘、远程共享和打印。具体清单见 sources.json。
- 原有 14 篇本地企业政策：保留 TL-403、自定义产品、服务台流程、审批、SLA 和分级访问。保留原文、source、版本和 ACL，原有评测题与证据坐标仍可匹配。
- 真实技术指南按各原始页面明确声明的 CC BY-SA 3.0 使用。转换后的指南保留出处、原始作者/译者信息、许可证和修改说明，继续按相同许可提供；这项许可不替代项目代码或其他文档各自的许可。
- 并非 GNOME 的企业案例库。GNOME 的操作方法不授予企业安装、改权限、关闭防护或修改网络的权限，也不代表 Windows/macOS 的操作方法。
- sources.lock.json 固定本次原始 HTML 的 SHA-256。上游页面改变时，重新下载会报错，必须有意识地建立新快照和版本，不能静默更新旧评测资料。

来源：[GNOME 官方简体中文帮助](https://help.gnome.org/gnome-help/index.html.zh_CN)，[许可](https://creativecommons.org/licenses/by-sa/3.0/)。

## 准备与导入

在项目根目录、已安装 requirements.lock 的 Python 环境运行：

~~~powershell
python scripts/enterprise_corpus.py fetch
python scripts/enterprise_corpus.py build
python scripts/enterprise_corpus.py verify
docker compose up -d --wait vectors
python scripts/enterprise_corpus.py import
~~~

原始网页、转换后的 Markdown、NOTICE、manifest 和 import-report 都在 data/enterprise-zh-2026-10-16-v1/，不默认提交上游网页或数据库到 Git。

导入使用项目默认 PostgreSQL 地址；也可设置 KNOWLEDGEOPS_DATABASE_URL 或传入 --database-url。目标默认 ko_enterprise_zh，只接受以 ko_enterprise_ 开头的独立 schema。原有 public、ko_samples、历史会话及审批不被清理。重复导入不会恢复已撤销的文档/工具权限，也不会覆盖已修改的业务值。

导入完成包含解析结果、800 字符结构切块、Agent 表、演示业务数据和 SELECT-only 业务角色。它不调用任何模型，不生成假向量，不审批或创建工单。

启动该空间：

~~~powershell
python scripts/enterprise_workbench.py
~~~

也可以在原工作台连接设置中选择 ko_enterprise_zh。公开指南的上传者为工程师；原有全员政策的上传者为普通员工，IT 支持专属政策的上传者为支持工程师。工作台批量索引按上传者筛选，因此需切换这三种身份分别完成对应文档索引。新建真实 Embedding 集合（例如 enterprise-zh-v1），填写自己的模型连接和维度，按工作台说明构建索引。enterprise-pending-embedding 是未配置的占位集合，没有向量，不能作为已就绪索引。保留已有回答模型配置；启用 Agent 仍需真实模型、单价和只读业务连接。

企业空间启动器只在未显式配置时设置本地只读演示账号。自定义数据库应通过 KNOWLEDGEOPS_AGENT_BUSINESS_DATABASE_URL 显式指定相同数据库和 schema 的只读业务 DSN。

## Agent 任务覆盖

| 场景 | 知识与业务来源 | 正确结果 |
| --- | --- | --- |
| A001 查 TL-403 流程、查询本人合规状态 | 保留的 VPN/终端政策 + device_status | 区分文档规则与本次模拟状态，给出有据建议 |
| A002 工单耗时计算和目标比较 | ticket_durations + calculator | 使用本次查询数值计算；默认演示值 12 + 28 = 40 分钟，目标 60 分钟 |
| A003 权限请求含糊 | 请求上下文 | needs_clarification，不编造系统或权限 |
| A004 检索超时、A005 重复无进展 | 执行预算、工具结果 | 有界失败，不伪报完成 |
| A006 拒绝审批、A007 待审批重启 | 持久化审批状态 | 拒绝时零新增；重启后仍等待原审批 |
| A008 提交后恢复 | 动作账本与工单事务 | 恢复原结果，工单仅创建一次 |
| A009 参数变化、A010 权限撤销 | 审批摘要和执行时重新授权 | 旧审批不得授权新参数；无权限不得提交 |

正常任务可使用现有工具完成；异常场景的“通过”指到达上表正确终态，不是强制 completed。当前 Agent 支持查询本人设备、账号安全、已提交工单及独立耗时，执行算术、检索和创建本地模拟工单，不执行重置密码、修复终端或授予真实权限。

可用演示消息：

1. 查找 TL-403 处理流程，再查询本人设备合规状态并给出建议。
2. 查询本人工单各阶段耗时，计算总耗时并与目标比较。
3. 查询 Linux/GNOME 终端连接企业 VPN 的设置步骤；先说明需要向管理员索取哪些连接信息。
4. 我的设备补丁未更新导致无法访问内部资源。先查规则和本人设备状态，再提出一个包含排障信息的模拟工单供我审批。

真实模型可能需要补充信息，不能保证每次自然语言任务都成功。只在 UI 上审阅并批准具体动作后才建单；本文不代表审批授权。

## 验证口径

~~~powershell
python scripts/run_tests.py -q
node --test tests/unit/*.mjs
~~~

本次增加正文提取与隔离导入回归。现有 A001–A010 是离线协议与状态机回归，模型使用测试替身，不等于真实模型的任务成功率。部署后还需在真实模型和已索引的企业空间实际运行演示任务。原有 questions.jsonl 继续验证本地政策；新增外部指南的答案质量需要独立人工标注。
