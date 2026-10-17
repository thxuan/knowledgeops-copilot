export function node(tag, cls='', text='') {
  const n=document.createElement(tag); if(cls)n.className=cls; n.textContent=String(text??''); return n;
}
export function action(label, callback, cls='secondary-button') {
  const b=node('button',cls,label);b.type='button';b.addEventListener('click',callback);return b;
}
export function timeLabel(value) {
  return typeof value==='number'&&Number.isFinite(value)?new Date(value*1000).toLocaleString('zh-CN',{hour12:false}):'历史记录未保存时间';
}
export function ticketStatus(ticket){return ticket.workflow?.status_label||({queued:'待受理',pending_triage:'待人工分诊',pending_approval:'待业务审批',approved:'审批通过 · 未执行',rejected:'申请已拒绝',created:'已创建 · 待分配'})[ticket.status]||'状态待核对';}
export function ticketLink(id, label='查看工单详情') {
  const a=node('a','secondary-button',label);a.href='/workbench/tickets/'+encodeURIComponent(id);return a;
}
export function agentFailureMessage(code){
  return ({UNPRICED_TOOL:'未配置 Embedding 单价，费用预算检查已阻止本轮工具执行。请在连接设置中填写 Embedding 单价并应用，然后重新发起任务。',UNBOUNDED_RERANK_COST:'当前重排配置无法预估费用。请关闭本次任务的重排后重新发起任务。'})[code]||('任务执行失败：'+(code||'请查看处理过程'));
}
const labels={search_knowledge_base:'查阅知识库',query_business_data:'查询本人业务状态',calculator:'计算结果',create_ticket:'整理工单内容'};
export function toolSummary(trace) {
  const r=trace.result||{},args=trace.arguments||{};
  if(trace.status==='failed')return '调用失败：'+(r.error||'未取得结果');
  if(trace.name==='search_knowledge_base')return r.untrusted_evidence?.length?'已找到 '+r.untrusted_evidence.length+' 条授权知识依据':'未找到可用知识依据';
  if(trace.name==='query_business_data'){
    const d=r.data||{};
    if(r.query==='device_status')return '设备合规：'+(d.compliant===true?'通过':d.compliant===false?'未通过':'未知')+(d.reason?' · '+d.reason:'')+'（模拟数据）';
    if(r.query==='account_status')return '账号：'+(d.account_enabled===true?'已启用':d.account_enabled===false?'已停用':'未知')+' · MFA 方法 '+(d.mfa_methods_count??'未知')+' 种（模拟快照）';
    if(r.query==='ticket_status')return '本人共 '+d.total+' 条工单，本次返回 '+(d.items?.length||0)+' 条；处理人和完成时间以记录为准（本地模拟工单）';
    if(r.query==='ticket_durations')return '分诊 '+(d.triage_minutes??'未知')+' 分钟 · 处理 '+(d.resolution_minutes??'未知')+' 分钟 · 目标 '+(d.target_minutes??'未知')+' 分钟（模拟数据）';
    return '已取得本人业务查询结果（模拟数据）';
  }
  if(trace.name==='calculator')return (args.left??'?')+' '+(args.operator??'?')+' '+(args.right??'?')+' = '+(r.value??'未知');
  if(trace.name==='create_ticket')return '工单已提交';
  return '已取得工具结果';
}
export function traceDetail(trace, onCitation) {
  const detail=node('details','step-evidence');detail.dataset.detailKey='evidence-'+trace.call_id;detail.append(node('summary','','查看依据'));
  for(const hit of trace.result?.untrusted_evidence||[]){
    const c=hit.citation||{},text=c.title||c.source||hit.source||'知识来源';
    const row=node('div','source-reference');
    if(onCitation&&c.chunk_id&&c.chunk_set_id)row.append(action(text,()=>onCitation(c),'text-button'));
    else row.append(node('span','',text));
    row.append(node('small','field-help',c.version?'版本 '+c.version:''));detail.append(row);
  }
  if(trace.result?.query==='ticket_status')for(const ticket of trace.result.data?.items||[]){
    const row=node('div','source-reference');row.append(ticketLink(ticket.ticket_id,ticket.title||'查看工单'));detail.append(row);
  }
  const raw=node('details','technical-detail');raw.dataset.detailKey='raw-'+trace.call_id;raw.append(node('summary','','工具原始记录'),node('pre','',JSON.stringify({arguments:trace.arguments,result:trace.result},null,2)));
  detail.append(raw);return detail;
}
export function progressView(agent,onCitation) {
  const terminal=['completed','cancelled','failed'].includes(agent.status),box=node(terminal?'details':'section','task-progress');box.setAttribute('aria-label','任务处理过程');
  const completed=(agent.trace||[]).filter(trace=>trace.name!=='create_ticket'&&trace.status!=='failed').length;
  if(terminal)box.dataset.detailKey='progress';
  box.append(node(terminal?'summary':'h4','',terminal?`处理记录 · ${completed} 步已完成`:'处理过程'));const list=node('ol','task-steps');
  const done=new Set();
  for(const trace of agent.trace||[]){
    if(trace.name==='create_ticket')continue;
    done.add(trace.call_id);const failed=trace.status==='failed',item=node('li','task-step '+(failed?'step-failed':'step-done'));
    item.append(node('span','step-mark',failed?'!':'✓'));
    const body=node('div','step-body');body.append(node('strong','',labels[trace.name]||trace.name),node('p','',toolSummary(trace)),traceDetail(trace,onCitation));item.append(body);list.append(item);
  }
  let activeTool=false;
  for(const e of agent.events||[]){
    if(e.type!=='tool_start'||done.has(e.call_id)||e.name==='create_ticket')continue;
    done.add(e.call_id);const running=agent.status==='running';const blocked=['UNPRICED_TOOL','UNBOUNDED_RERANK_COST'].includes(agent.outcome);activeTool ||= running;const item=node('li','task-step '+(running?'step-active':'step-failed'));
    item.append(node('span','step-mark',running?'…':'!'),node('div','',(labels[e.name]||e.name)+' · '+(running?'进行中':blocked?'预算检查未通过，本轮未执行':'未取得完成结果')));list.append(item);
  }
  if(currentProposal(agent)){const expired=agent.pending.expires_at*1000<=Date.now();const item=node('li','task-step step-waiting');item.append(node('span','step-mark',expired?'!':'Ⅱ'),node('div','',expired?'工单预览已过期 · 需要重新发起任务':'工单内容已整理 · 等待你确认'));list.append(item);}
  else if(agent.status==='running'&&!activeTool){const item=node('li','task-step step-active');item.append(node('span','step-mark','…'),node('div','',(agent.trace||[]).some(t=>t.status==='committed')?'正在整理提交结果':'正在分析已有信息与下一步行动'));list.append(item);}
  if(!list.childElementCount)list.append(node('li','field-help',agent.outcome==='needs_clarification'?'需要补充信息后继续处理。':'尚无工具处理记录。'));
  box.append(list);return box;
}
export function currentProposal(agent){
  return agent.status==='waiting_approval'?agent.pending:null;
}
export function proposalDecision(p,approved,now=Date.now()){
  if(!approved&&p.expires_at*1000<=now)return {suffix:'cancel'};
  return {suffix:'approval',body:{action_id:p.action_id,version:p.version,digest:p.digest,approve:approved}};
}
export function proposalView(p,{approve,revise}) {
  const box=node('section','ticket-proposal');box.setAttribute('aria-label','工单预览与提交');
  const expired=p.expires_at*1000<=Date.now();
  const top=node('div','ticket-card-heading');top.append(node('span','eyebrow','工单预览'),node('span','ticket-badge waiting',expired?'预览已过期':'尚未提交'));box.append(top,node('h3','',p.arguments.title));
  const preview=node('div','proposal-preview');preview.append(node('p','field-help','问题描述'),node('p','ticket-description',p.arguments.description));
  preview.append(node('p','field-help','请核对故障事实；如已知，可补充资产编号、发生时间及已尝试步骤。'));
  preview.append(node('p','proposal-impact','确认后将创建 1 条本人名下的本地模拟工单；不会修改设备配置或授予账号权限。'));
  const policy=p.classification;
  if(policy){const classification=node('section','ticket-classification');classification.append(node('strong','',`服务端分类：${policy.category_label}`),node('p','field-help',policy.reason),node('p','field-help',policy.category==='prohibited'?'此操作不可提交或通过审批放行。':`提交后：${policy.status_label}。提交确认不等于业务审批，审批通过也不会自动执行。`));preview.append(classification);}
  const actions=node('div','actions proposal-actions');
  const confirm=action('确认提交',()=>approve(true),'primary-button'),edit=action('修改内容',()=>{preview.hidden=true;form.hidden=false;title.focus();}),reject=action(expired?'关闭过期预览':'暂不提交',()=>approve(false),'secondary-button danger-button');
  confirm.disabled=expired||policy?.category==='prohibited';edit.disabled=expired;actions.append(confirm,edit,reject);preview.append(actions);
  const form=node('form','proposal-editor');form.hidden=true;
  const titleLabel=node('label','field','工单标题'),title=node('input');title.value=p.arguments.title;title.required=true;title.minLength=3;title.maxLength=120;titleLabel.append(title);
  const descriptionLabel=node('label','field','问题描述'),description=node('textarea');description.value=p.arguments.description;description.rows=6;description.required=true;description.minLength=3;description.maxLength=1000;descriptionLabel.append(description);
  const buttons=node('div','actions'),save=node('button','primary-button','保存修改并返回预览');save.type='submit';
  buttons.append(save,action('取消修改',()=>{title.value=p.arguments.title;description.value=p.arguments.description;form.hidden=true;preview.hidden=false;edit.focus();}));
  const error=node('p','error-text');error.setAttribute('role','alert');
  form.append(titleLabel,descriptionLabel,node('p','field-help','保存后需再次确认创建；保存本身不会提交工单。'),error,buttons);
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(!title.value.trim()||!description.value.trim()){error.textContent='请填写有效的标题和问题描述。';return;}
    for(const b of form.querySelectorAll('button'))b.disabled=true;
    try{await revise({title:title.value,description:description.value});}finally{for(const b of form.querySelectorAll('button'))b.disabled=false;}
  });
  box.append(preview,form,node('p','ticket-footnote',expired?'预览已过期，关闭后可重新发起任务；本次不会创建工单。':'版本 '+p.version+' · 有效至 '+timeLabel(p.expires_at)+' · 确认后创建一条本地模拟工单。'));
  return box;
}
export function receiptView(trace,events=[]) {
  const r=trace.result||{},box=node('section','ticket-receipt');box.setAttribute('aria-label','工单创建回执');
  box.append(node('span','ticket-badge success','✓ 创建成功'),node('h3','',trace.arguments?.title||r.title||'模拟工单'));
  box.append(node('p','ticket-reference','工单编号：'+r.ticket_id),node('p','field-help','提交时间：'+timeLabel(r.created_at??events.find(e=>e.type==='tool_end'&&e.result?.ticket_id===r.ticket_id)?.time)));
  if(trace.arguments?.description)box.append(node('p','ticket-description',trace.arguments.description));
  if(r.classification)box.append(node('p','ticket-classification',`${r.classification.category_label} · ${r.classification.status_label}`));
  box.append(node('p','field-help','已提交本地模拟工单，不代表业务审批通过或已执行。后续处理进展请以工单详情为准。'),ticketLink(r.ticket_id));return box;
}
