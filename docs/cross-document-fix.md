# 跨文档证据覆盖与回答遗漏修复

日期：2026-10-12。用户纠正范围后，默认已恢复generated自然语言回答。证据窗口和短引用改进保留；grounded整段摘录是超出本次引用优化目标的尝试，仅保留为可选模式。跨文档结论与引用的语义绑定尚未完成验收。历史Day8/Day9结果和冻结配置保留原样。

## 问题与修改

原开发集X001必要文档已在候选20内，但同一VPN文档占满最终Top5，操作系统最低版本未进入生成输入。X003的会话终止和擦除决策在输入中，却被生成遗漏。旧保留测试X004另有缺章和执行时授权遗漏。因此分别调整证据进入上下文的方式与生成复核。

- `/chat`默认top_k从5改为20，范围仍为1..20。`/retrieve`默认5、Agent搜索工具默认3/最大5保持原契约；此次修复目标是已评测的RAG问答路径。调用方显式传5仍使用5，不能期待自动变为20。
- 授权候选按文档轮流进入上下文，同一文档内部保持排名，最高排名片段仍在最前。片段整体保留、去重；预算不够时跳过整块并记录omitted_evidence_ids。不根据题号、预期答案或gold来源选证据。
- 保留原始来源、版本和章节路径。模型输入中的引用使用E1/E2等短编号，服务端映射回原Chunk ID并检查仅引用实际输入证据；API引用链接和数据库记录继续使用真实ID。兼容旧模型客户端返回的已授权canonical ID。
- 生成明确覆盖当前问题相关的步骤、前提、禁止事项、时限及决策权限，区分标准流程和例外流程。compatible模型得到合法草稿后，用同一批证据再次复核；不向模型提供评测答案。Fake不承担语义审核。
- 实测表明模型自己复核仍会遗漏或改反规定。曾将compatible默认设为answer_mode=grounded（现已恢复generated）：模型选择/排序引用，程序返回被选中片段的完整原文，不保留模型改写的事实。原文一致性可程序检查，选段相关性/完整性、文档本身真实性仍需审核。回答更长且可能重复，不能把这种取舍说成自然语言生成能力全面提升。
- 已选文档里，如果问题明确提到的章节主题尚未出现在选段中，则从同一实际上下文补入匹配章节。例：X004已选紧急变更却漏冻结期，通用的标题主题检查补入冻结期；不新增检索、不越过权限范围、不读取gold。返回段落超过既有长度限制时拒绝，不截断责任人或条件。
- 两轮生成共享一次格式修复，总上限3次。复核前重新授权、提交时再检查。模型复核仍不是事实正确性的程序证明，引用成员有效也不证明语义支持。
- 输入预算默认32000（保守UTF-8字节加消息框架估计），为证据和草稿复核留空间；输出上限仍2048。复核追加草稿若超过预算，保留已校验草稿并标记skipped_budget，不静默删证据。每次生成超时默认60秒，总调用次数有界；普通聊天断连取消继续生效。
- context增加实际evidence_chunk_ids、预算遗漏ID、coverage_review、固定错误码validation_failures。错误诊断不包含原始非法输出或密钥。
- CLI评测另外统计input_evidence_metrics，区分召回结果与实际进入模型的证据；历史缺此字段的报告不自动补成绩。

仅扩证据窗口并不等于提升同一个K下的排序质量。本方案为当前14份样本文档的覆盖与成本取舍，RRF/BM25排序本身未改变，查询拆分、多查询召回、语义去重、父段落扩展尚未实现。增加输入与复核会增加模型调用及延迟，可显式top_k=5、设置KNOWLEDGEOPS_GENERATION_COVERAGE_REVIEW=false或KNOWLEDGEOPS_GENERATION_ANSWER_MODE=generated作对照。generated仍可能产生遗漏或错误改写，不当作已完全修复。检索对照CLI和RAG评测CLI默认top_k同步到20，原Top5结果不改。

## 复现

历史真实候选回放（无API/数据库调用；只接受dev）：

```powershell
& 'D:\leetcode\.venv-knowledgeops\Scripts\python.exe' -B scripts/replay_evidence_coverage.py --candidates docs/experiments/day9/20261004-145415-133ac4e8/results.jsonl --baseline docs/experiments/day8/20261004-152805-5646ce30/results.jsonl
```

真实API评测使用已配置百炼的本机Uvicorn，Key留在服务器进程，不需要交给评测脚本。每题分别读取检索结果及发起全新问答，按context中的真实ID核对实际输入；索引变化而无法关联时记错误，不混算成功。默认dev20题、每题一次，不提供多次波动或泛化结论。模型usage/计费未通过此API暴露，结果记录null。

```powershell
& 'D:\leetcode\.venv-knowledgeops\Scripts\python.exe' -B scripts/verify_rag_api.py --api-url http://127.0.0.1:18080 --scope dev
& 'D:\leetcode\.venv-knowledgeops\Scripts\python.exe' -B scripts/verify_rag_api.py --api-url http://127.0.0.1:18080 --scope regression --question-ids X004 X005
```

已经分析过的旧test题只算回归检查，不能称为新的独立保留测试。报告包含逐题答案、证据和复核模板；预期答案只在本地评价用，从不发送到/chat或/retrieve。

## 运行位置

本轮新代码在本机18080运行；8000由knowledgeops-copilot-app-1容器占用。容器镜像仍是旧版本，需要另行构建更新后才会含本次修改。真实样本元数据已迁到PostgreSQL的ko_samples schema，当前统一设置KNOWLEDGEOPS_DATABASE_URL，不再设置旧SQLite环境变量。

## 最终结果

| 验证 | 结果与范围 |
| --- | --- |
| 最新完整代码回归 | 278 passed、0失败/错误/跳过，含真实本地PG/pgvector；既有弃用警告1条 |
| 历史真实dev候选回放 | 实际输入必要来源由1/3到3/3；17可回答题平均来源Recall .9510→1.0，章节字符覆盖 .9062→.9993；窗口5→20，不能解释为同K排序改善 |
| 中间generated真实dev20题 | 17 answered/3 refused、0 rejected/error，来源全召回3/3；X003人工审核仍发现遗漏/错误改写，因此未记全量语义通过 |
| 最新grounded真实回归5题 | X001–X005各一次，5 answered、0 error/rejected/refused；必要来源5/5，返回每条claim与引用原文完整文本一致 |
| 补充离线渲染 | 22条中间版本已存真实API响应通过原文渲染，0新API调用；不是新真实dev运行 |

助手逐条核对：X001包含Windows11 23H2；X003包含活动会话终止、拒绝MFA/修改密码、擦除由安全团队判断；X004包含冻结期例外、紧急变更批准/实施/回滚/次日复核、紧急访问60分钟与执行时重新拒绝；X005包含硬盘/整机更换风险、未同步数据不保证恢复、网页确认。已知遗漏在本轮返回原文中得到保留。

X002的rubric有一处需谨慎：数据分类表“仅指定收件人并加密”明确位于L3邮件列，TeamDrive列规定受限空间/7天；外部分享章节明确所有者批准/7天/禁止匿名。最终回答保留这些原文，不凭借关键词命中宣称已证明TeamDrive专属的指定收件人规则。评测标注或示例制度的歧义应另行澄清，不能为了得分编造规则。

第一版真实dev为16 answered/3 refused/1 rejected；旧X004回归首次GENERATION_TIMEOUT。短引用版本消除了本轮格式拒绝，但其自由改写仍有X003语义失败。所有阶段保留，不以最新覆盖率掩盖旧失败。最终grounded模式没有再做全量20题云评测，只做5题真实回归与22响应离线渲染；没有新独立保留测试、三次稳定性实验或全量人工语义成绩。真实调用经过用户启动的本机服务，Key没有读取或保存到报告；API未暴露provider usage和价格，费用/Token记录null。

[最新核验](experiments/cross-document/20261012-evidence-window/verification.json)、[最新真实回归](experiments/cross-document/20261012-112835-f7f11fab/summary.json)、[逐题答案及证据](experiments/cross-document/20261012-112835-f7f11fab/results.json)、[助手原文复核](experiments/cross-document/20261012-112835-f7f11fab/assistant-source-review.json)、[中间真实dev](experiments/cross-document/20261012-110923-fdd2605f/summary.json)。


## 用户纠正：引用优化与回答方式分开（2026-10-12）

用户指出目标是跨文档引用优化。助手将默认回答改为原文摘录偏离范围，已恢复GenerationSettings、环境配置与.env.example的generated默认值；grounded仅供显式选择。已有原文模式回归是历史实验，不重新解释成自然语言引用验收通过。

本次应验证三个层次：必要来源召回、问题相关章节进入实际输入、自然语言结论由所引证据支持。扩大窗口已经证明前两层的覆盖改善，短编号减少引用标识复制负担；合法ID和原文摘录不能替代第三层语义检查。generated版本的X003错误仍保留，引用语义正确性不能宣称全部修复。


## 后续：覆盖选择与问题拆分

新增可选coverage/multi_query模式，默认ranked+Top20保持。新开发集同窗口Top8对照必要来源1/3→3/3、17题章节覆盖90.6%→95.1%，追加3题来源仍3/3；未评估最终自然语言答案和引用支持率。单独coverage@5存在章节覆盖倒退，不作为默认改进发布。完整方法、开销和复现见[证据选择试验](evidence-selection.md)。
