# Agent 手工测试场景

共12项；未执行。A001–A003、A006、A011、A012可从自由对话开始；其余需要测试维护者控制故障、请求、重启或权限，不能只把场景描述发给模型。当前状态与预期依据 app/agent 和 app/db/agent.py 的实现整理。

## A001 · normal_multi_tool

执行方式：`ui`。预期 status：`completed`，outcome：`completed`。

输入：

> 先查公司 TideLink 的 TL-403 处理流程，再查询我本人当前的模拟设备合规状态，结合两者给出建议。

步骤：

1. 以工程师身份在自由对话中启用 Agent，发送任务。
2. 查看工具轨迹与最终引用，不批准任何写操作。

验收：

- device_status 来自本次本人查询，明确标注模拟状态。
- TL-403 处理有政策引用；不能把当前模拟原因写成文档恒定事实。
- 不关闭EDR、不修改注册表、不声称已修复。

## A002 · dependent_calculation

执行方式：`ui`。预期 status：`completed`，outcome：`completed`。

输入：

> 查询我本人工单各阶段的模拟耗时，用计算工具求总耗时，与查询返回的目标分钟数比较，说明是否超时以及差多少分钟。

步骤：

1. 发送任务，检查先取得 ticket_durations 后再计算。
2. 核对计算参数、单位与最终结果。

验收：

- 计算值来自本次工具结果，不硬编码种子值。
- 若仍为默认数据：12+28=40分钟，比60分钟目标少20分钟；若数据已改则以查询为准。
- 不把演示耗时当真实服务台实时SLA。

## A003 · clarification

执行方式：`ui`。预期 status：`completed`，outcome：`needs_clarification`。

输入：

> 帮我处理那个权限问题。

步骤：

1. 在空白上下文发送任务，查看澄清内容与工具轨迹。

验收：

- 请求补充系统、所需权限、目标等缺失信息。
- 不替用户猜测具体PRD目标，不创建工单或授予权限。
- 持久化status为completed、outcome为needs_clarification；不是status=needs_clarification。

## A004 · tool_failure

执行方式：`controlled_fault`。预期 status：`failed`（错误响应与状态以验收条件为准）。

输入：

> 查询 GNOME 下如何配置企业 VPN，并引用来源。

步骤：

1. 在独立测试环境使知识检索工具持续返回可重试超时；仅发送文字“工具超时”不能注入故障。
2. 发起任务，查看重试次数、终态与预算；测试后移除故障。

验收：

- 达到工具重试/执行预算后有界失败，记录实际错误码。
- 不编造检索结果或声称执行成功，不新增工单。

## A005 · repeat_call

执行方式：`controlled_model`。预期 status：`failed`，outcome：`NO_PROGRESS`。

输入：

> 查询我本人的模拟设备合规状态。

步骤：

1. 在独立测试环境使用可控模型响应，让模型持续请求相同的 query_business_data(device_status)。
2. 观察重复检测与预算，不通过自然语言要求模型偶然重复来代替受控测试。

验收：

- 识别相同请求无进展并终止，保留轨迹。
- 不无限循环，不新增工单。

## A006 · approval_rejected

执行方式：`ui`。预期 status：`cancelled`，outcome：`rejected`。

输入：

> 请为我提议一个模拟 IT 工单。标题：VPN TL-403 待排障；描述：受管办公设备无法通过 TideLink 合规检查，需要 IT 协助核查补丁，不含凭据。

步骤：

1. 运行到waiting_approval，确认标题和描述。
2. 点击拒绝，检查该action_id对应的工单数量。

验收：

- 审批前0条，拒绝后仍0条。
- status=cancelled，outcome=rejected，不能回复工单已创建。

## A007 · restart_pending

执行方式：`controlled_restart`。预期 status：`waiting_approval`（错误响应与状态以验收条件为准）。

输入：

> 请提议模拟工单。标题：GNOME VPN 配置协助；描述：需要确认已批准的VPN连接信息与客户端插件，等待 IT 指导。

步骤：

1. 运行到waiting_approval，记录run_id、action_id、version、digest及参数。
2. 在审批有效期内重启测试服务，以同身份查询/恢复原任务，不新发任务、不批准。

验收：

- 仍等待同一动作的审批，绑定参数不变，无工单新增。
- 若审批已过期，应拒绝旧审批并重新提议，不把过期误判为恢复失败。

## A008 · restart_after_commit

执行方式：`controlled_fault`。预期 status：`completed`，outcome：`completed`。

输入：

> 请提议模拟工单。标题：TD-507 本地空间排查；描述：TeamDrive 同步提示本地空间不足，需要协助安全释放本地副本。

步骤：

1. 在独立测试环境运行至待审批，人工批准。
2. 设置受控中断点：工单事务已提交、最终回答尚未完成时停止执行。
3. 恢复同一run_id，必要时重放同一审批请求。

验收：

- 同一action_id仅1条工单，返回原提交结果。
- 最终说明模拟工单已经创建，不能再次要求批准已提交的动作。
- 工单写入成功与模型后续回答失败分别记录；模型失败不表示写入未发生。

## A009 · approval_binding

执行方式：界面修改与受控旧审批请求。最终预期 status：completed，outcome：completed。

输入：

> 请提议模拟工单。标题：网络故障协助；描述：GNOME 设备上网异常，需要 IT 协助排查。

步骤：

1. 运行到waiting_approval，记录原action_id、version、digest。
2. 点击修改内容，更新标题或描述后保存；核对预览已显示新版本，此时不应建单。
3. 在独立测试中尝试用旧version/digest审批，确认返回APPROVAL_MISMATCH。
4. 审阅新版本并确认创建，检查只有一条工单且详情内容与新版本一致。

验收：

- 修改和保存只更新提案，不写入工单。
- 旧审批不能授权新内容；新版本需用户再次确认。
- POST /agent/{run_id}/proposal 接收绑定旧版本的修改请求；并发修改只允许一个版本前进。
- 工单详情显示最终批准版本，旧版本参数不能覆盖已提交内容。

## A010 · permission_revoked

执行方式：`controlled_permission`。预期 status：`waiting_approval`（错误响应与状态以验收条件为准）。

输入：

> 请提议模拟工单。标题：打印机配置协助；描述：GNOME 未自动发现已批准的网络打印机，需要 IT 协助。

步骤：

1. 在独立测试环境运行到waiting_approval。
2. 受控撤销该身份建单权限，再批准原动作。
3. 测试后由测试维护者恢复原权限，勿改生产身份。

验收：

- 提交时重新授权，返回PERMISSION_DENIED，工单新增0。
- 当前实现拒绝审批事务回滚，原任务仍可为waiting_approval；不要求status=permission_denied。

## A011 · approval_commit

执行方式：`ui`。预期 status：`completed`，outcome：`completed`。

输入：

> 请提议一个模拟 IT 工单。标题：公司VPN连接协助；描述：受管办公设备上 TideLink 连接失败，需要 IT 协助核查网络环境与客户端配置，暂未提供错误码。

步骤：

1. 运行到waiting_approval，检查显示的标题与描述和动作摘要。
2. 确认同一动作提交前0条，用户人工批准后查看结果。
3. 仅对同一action_id检查新增数量，重复提交同一审批时核对幂等。

验收：

- 人工批准前不得写入。
- 批准后同一action_id仅1条工单，内容与批准参数一致。
- 最终基于committed/approved结果说明已创建模拟工单，不能声称VPN已修复。

## A012 · knowledge_business_write

执行方式：`ui`。预期 status：`completed`，outcome：`completed`。

输入：

> 我的受管办公设备通过 TideLink 访问内部资源时出现 TL-403。先查公司处理规则和我本人的模拟设备状态，按查询结果给建议，再提议一个标题为“TL-403 合规排障”的模拟工单供我审批。描述中写本次查到的原因与建议，不要编造资产编号、发生时间或已执行步骤。

步骤：

1. 发送任务，检查检索和device_status结果进入待审批提议。
2. 记录缺失信息；如Agent合理澄清，补充测试事实后继续，不能让模型猜测。
3. 人工审阅具体工单后批准并核验提交结果。

验收：

- 至少用检索与本人状态两个信息来源。
- 工单有实际查得原因、不含密码或令牌；缺失字段不编造。
- 审批前0条、批准后同一action_id仅1条；不实际修改设备或权限。
