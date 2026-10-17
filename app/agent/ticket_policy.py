"""Conservative local ticket routing; submission never authorizes execution."""
import re
import unicodedata

POLICY_VERSION = "local-ticket-policy-v1"
CATEGORY_LABELS = {"incident": "普通故障", "standard": "标准服务请求", "sensitive": "敏感申请", "uncertain": "待人工分诊", "prohibited": "禁止操作"}
STATUS_LABELS = {"queued": "待受理", "pending_triage": "待人工分诊", "pending_approval": "待业务审批", "approved": "审批通过 · 未执行", "rejected": "申请已拒绝"}
REVIEW_SCOPES = {"access_review": "权限申请审批", "security_review": "安全例外审批", "production_review": "生产变更审批"}
RISK_RULES = (
    ("access_review", r"管理员权限|(?:root|sudo)\s*权限|提权|权限提升|提升权限|受限(?:数据|资源|系统)|跨部门.{0,8}(?:访问|数据)|(?:申请|开通|授予|新增).{0,12}(?:权限|生产访问)|admin(?:istrator)?\s+(?:access|privileges)|privilege\s+escalation"),
    ("security_review", r"(?:关闭|停用|禁用|跳过|绕过|豁免).{0,12}(?:EDR|MFA|防护|杀毒|认证|合规|检查)|安全例外|合规例外|例外放行|(?:disable|bypass).{0,15}(?:edr|mfa|security|compliance)"),
    ("production_review", r"(?:生产|线上|production|prod).{0,20}(?:变更|部署|重启|停机|更新|修改|删除|发布|访问|权限|deploy|restart|update)|(?:变更|部署|重启|停机|更新|修改|删除|发布).{0,12}(?:生产|线上)"),
)
PROHIBITED = r"绕过.{0,8}(?:审批|审计)|(?:删除|清除|篡改).{0,8}(?:审计日志|审计记录)|(?:窃取|盗取).{0,8}(?:凭据|密码|密钥)|越权.{0,8}(?:读取|访问)|bypass.{0,12}(?:audit|approval)|steal.{0,12}(?:credentials|password)"
STANDARD = r"(?:更新|安装|部署|升级).{0,8}(?:合规补丁|标准补丁|已批准补丁)|(?:补丁更新|更新补丁)|安装.{0,8}(?:已批准软件|公司软件目录)|approved\s+(?:patch|software)"
INCIDENT = r"(?:VPN|打印机|网络|软件|电脑|设备|登录|邮箱|客户端).{0,15}(?:故障|报错|失败|崩溃|连不上|无法|异常)|(?:排查|检查|修复).{0,12}(?:故障|报错|连接|打印|补丁状态)|(?:vpn|printer|login).{0,12}(?:error|failure|broken)"

def review_capabilities(identity):
    return identity.ticket_capabilities & frozenset({"triage", *REVIEW_SCOPES})

def classify_ticket(arguments, original_request=""):
    text = unicodedata.normalize("NFKC", "\n".join([original_request, arguments.get("title", ""), arguments.get("description", "")]))
    text = re.sub(r"[\u200b-\u200f\ufeff]", "", text)
    def positive(pattern):
        matches = list(re.finditer(pattern, text, re.I))
        return any(not re.search(r"(?:不要|禁止|不能|不得|不会|无需|不允许)(?:进行|执行)?$", text[max(0, match.start()-8):match.start()]) for match in matches)
    if positive(PROHIBITED):
        category, scopes, reason = "prohibited", [], "涉及绕过审批、审计或越权等禁止操作，不能通过工单放行。"
    else:
        scopes = [scope for scope, pattern in RISK_RULES if positive(pattern)]
        if scopes:
            category, reason = "sensitive", "涉及" + "、".join(REVIEW_SCOPES[scope] for scope in scopes) + "，须由具备全部对应能力且非申请人的人员审批。"
        elif re.search(PROHIBITED + "|" + "|".join(pattern for _, pattern in RISK_RULES), text, re.I):
            category, reason = "uncertain", "包含否定、引用或不明确的敏感操作描述，需要人工确认实际诉求。"
        elif re.search(STANDARD, text, re.I):
            category, reason = "standard", "仅进入标准服务请求受理队列；执行前仍须核对批准目录、资产范围与维护条件。"
        elif re.search(INCIDENT, text, re.I):
            category, reason = "incident", "普通故障排查，进入受理队列；若后续涉及敏感操作，必须升级审批。"
        else:
            category, reason = "uncertain", "操作、资源或影响范围尚不明确，不能默认作为普通工单执行。"
    status = {"prohibited": "rejected", "sensitive": "pending_approval", "uncertain": "pending_triage"}.get(category, "queued")
    return {"category": category, "category_label": CATEGORY_LABELS[category], "status": status,
            "status_label": STATUS_LABELS[status], "required_capabilities": scopes,
            "reason": reason, "policy_version": POLICY_VERSION, "execution_allowed": False}

def proposal_policy(state, arguments):
    request = next((message["content"] for message in reversed(state.get("messages", [])) if message.get("role") == "user"), "")
    return classify_ticket(arguments, request)
