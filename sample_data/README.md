# 澄湾科技内部 IT 知识库（模拟数据）

这是一套专门为 KnowledgeOps Copilot 项目编写的虚构企业技术支持资料。公司、人员、域名、系统、地址、编号和流程均为模拟内容，不对应真实组织。

## 场景

澄湾科技是一家约 680 人的软件公司，员工分布在上海、成都和远程办公地点。知识库服务于员工自助排障和 IT 服务台辅助处理，覆盖账号、终端、网络、协作工具、数据安全、工程访问和事件响应。

## 测试身份

| 用户 ID | 身份 | 可访问受众 |
| --- | --- | --- |
| `u_employee_001` | 普通员工林晓 | `all-employees` |
| `u_engineer_001` | 工程师陈北 | `all-employees`、`engineering-cn` |
| `u_support_001` | IT 支持工程师王宁 | `all-employees`、`it-support` |

所有身份属于租户 `tenant_clearwater`。当前运行权限由服务端身份和 document_access 用户 ACL 决定。audiences 用于说明样例身份，不能替代 ACL，也不接受模型或请求参数自行声明组成员关系。

## 文档约定

- 文档头部保存稳定的 `document_id`、版本、状态、分类、受众和责任人。
- 当前有效版本均为头部所列版本。后续测试文档更新时，旧版本必须失效。
- `all-employees` 文档对三种测试身份可见。
- `engineering-cn` 仅对工程师身份可见；`it-support` 仅对支持工程师身份可见。
- 文档中的 `*.example.internal` 是保留给本项目使用的虚构内部域名。
- 文档中的电话分机、IP 地址和设备编号均为模拟值。

## 文档清单

| 文档 ID | 文件 | 受众 | 主题 |
| --- | --- | --- | --- |
| KB-IT-001 | `01-it-service-catalog.md` | 全员 | 服务台、服务时间、优先级入口 |
| KB-ID-001 | `02-account-sso-mfa.md` | 全员 | OneGate、密码、MFA、锁定 |
| KB-NET-001 | `03-tidelink-vpn-guide.md` | 全员 | VPN 安装、连接和错误码 |
| KB-SEC-001 | `04-endpoint-compliance.md` | 全员 | 终端合规、补丁、加密和 EDR |
| KB-COL-001 | `05-email-and-files.md` | 全员 | 邮件、附件、文件分享和保留 |
| KB-ENG-001 | `06-engineering-access.md` | 工程组 | 开发、预发布和生产访问 |
| KB-OPS-001 | `07-incident-priority-and-escalation.md` | IT 支持 | 事件分级、响应和升级 |
| KB-BCP-001 | `08-backup-and-restore.md` | 全员 | 备份范围、RPO/RTO、恢复 |
| KB-DATA-001 | `09-data-classification.md` | 全员 | L1-L4 数据分类与处理要求 |
| KB-REM-001 | `10-remote-work-and-travel.md` | 全员 | 远程办公、公共网络和出差 |
| KB-APP-001 | `11-common-errors.md` | 全员 | 常见错误码和快速处理 |
| KB-LCM-001 | `12-device-lifecycle.md` | 全员 | 领用、维修、换机和报废 |
| KB-CHG-001 | `13-maintenance-calendar.md` | 全员 | 维护窗口、通知和冻结期 |
| KB-HRIT-001 | `14-onboarding-and-offboarding.md` | 全员 | 入职、转岗、离职和账号生命周期 |

## 使用边界

知识库用于回答政策和操作问题，不能替代实时监控、工单系统或资产数据库。设备当前状态、个人工单进度、某日实时故障、未写入文档的例外审批结果，都应调用相应业务工具或明确拒答。
