import {installTicketInbox} from './ticket-inbox.js?v=20261020-tab-selection';
import {auth,authenticatedFetch as fetch} from './auth-session.js';
import {node,action,timeLabel,ticketLink,progressView,ticketStatus} from './agent-view.js?v=20261017-ticket-policy';
const $=id=>document.getElementById(id);
let epoch=0,offset=0,scope=null;
const reviewMode=new URLSearchParams(location.search).get('review')==='1';
for(const [id,selected] of [['own-tickets',!reviewMode],['review-tickets',reviewMode]]){
 const button=$(id);button.className=selected?'primary-button':'secondary-button';button.setAttribute('aria-pressed',String(selected));
}
installTicketInbox({auth,fetch});
const scopeNames={access_review:'权限申请审批',security_review:'安全例外审批',production_review:'生产变更审批'};
try{const saved=sessionStorage.getItem('knowledgeops-identity');if([...$('identity').options].some(o=>o.value===saved))$('identity').value=saved;}catch{}
async function api(path,identity,workspace=scope,options={}){
 const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),20000);
 try{
  const res=await fetch(path,{...options,signal:controller.signal,headers:{'X-Local-User-Id':identity,...(options.body?{'Content-Type':'application/json'}:{}),...(workspace?{'X-Knowledge-Workspace':workspace}:{})}});
  const data=await res.json();if(!res.ok)throw new Error(data.error?.message||'读取工单失败');return data;
 }finally{clearTimeout(timer);}
}
async function source(c){
 const current=epoch,identity=$('identity').value;$('ticket-source-title').textContent=c.title||'引用原文';$('ticket-source-text').textContent='正在检查权限并读取原文…';$('ticket-source').showModal();
 try{const r=await api('/citations/'+encodeURIComponent(c.chunk_id)+'?chunk_set_id='+encodeURIComponent(c.chunk_set_id),identity);if(current===epoch)$('ticket-source-text').textContent=r.text;}
 catch(e){if(current===epoch)$('ticket-source-text').textContent=e.message;}
}
function field(label,value){
 const box=node('div','ticket-meta-item');box.append(node('dt','',label),node('dd','',value));return box;
}
function renderDetail(ticket){
 $('page-title').textContent='工单详情';
 const back=node('a','quiet-link',reviewMode?'← 返回待我处理':'← 返回我的工单');back.href='/workbench/tickets'+(reviewMode?'?review=1':'');
 const main=node('article','panel ticket-detail'),heading=node('div','ticket-card-heading');
 heading.append(node('span','eyebrow','本地模拟工单'),node('span','ticket-badge waiting',ticketStatus(ticket)));
 main.append(heading,node('h2','',ticket.title));
 const meta=node('dl','ticket-meta');meta.append(field('工单编号',ticket.ticket_id),field('提交时间',timeLabel(ticket.created_at)),field('提交身份',ticket.user_id),field('处理人',ticket.assignee||'未领取'),field('处理结果',ticket.resolution||'尚未记录'),field('确认提交版本',ticket.approval_version?'版本 '+ticket.approval_version:'见申请人提交记录'));
 main.append(meta,node('h3','','问题描述'),node('p','ticket-description',ticket.description),node('p','field-help','工单提交、业务审批和操作执行是三个独立环节。此系统只记录本地工单与审批，不会修改设备或账号，处理人与结果均来自人工记录，不代表设备或账号操作已自动执行。'));
 if(ticket.workflow){const policy=node('section','ticket-classification');policy.append(node('strong','',ticket.workflow.category_label+' · '+ticketStatus(ticket)),node('p','field-help',ticket.workflow.reason),node('p','field-help','所需审批能力：'+(ticket.workflow.required_capabilities?.map(name=>scopeNames[name]||name).join('、')||'无需敏感操作审批')));main.append(policy);}
 if(reviewMode&&ticket.review_actions?.length)main.append(decisionForm(ticket));
 const activity=activityForm(ticket);if(activity)main.append(activity);
 const history=node('section','panel ticket-history');history.append(node('h2','','处理与审批记录'));
 const timeline=node('ol','ticket-timeline'),names={accepted:'收到处理任务',waiting_approval:'整理工单，等待提交确认',proposal_revised:'用户修改了工单内容',tool_end:'申请人已确认提交，工单创建成功'};
 for(const e of ticket.timeline||[]){
  const li=node('li');li.append(node('strong','',names[e.type]||e.type),node('time','field-help',timeLabel(e.time)));
  if(e.version)li.append(node('p','field-help','提案版本 '+e.version));timeline.append(li);
 }
 history.append(timeline);
 const decisionNames={triage:'人工分诊',approve:'业务审批通过（未执行）',reject:'业务申请被拒绝',escalate:'升级至敏感操作审批',claim:'支持人员领取工单',comment:'处理留言',resolve:'处理人记录解决结果',close:'申请人确认关闭',reopen:'申请人重开工单'};
 for(const event of ticket.workflow?.history||[]){const item=node('li');item.append(node('strong','',decisionNames[event.action]||event.action),node('time','field-help',timeLabel(event.time)),node('p','field-help',event.actor+' · '+event.reason));timeline.append(item);}
 if(ticket.run_status==='failed'||ticket.run_status==='interrupted')history.append(node('p','notice','工单已成功写入，Agent 后续回复'+(ticket.run_status==='failed'?'失败':'中断')+'；无需重复建单。'));
 const evidence=node('section','panel ticket-history');evidence.append(node('h2','','建单前的排障依据'),progressView({trace:ticket.trace||[],status:'completed'},source));
 $('ticket-content').append(back,main,history);if(!reviewMode)$('ticket-content').append(evidence);
}
function decisionForm(ticket){
 const form=node('form','ticket-decision'),reason=node('textarea'),category=node('select'),error=node('p','error-text');error.setAttribute('role','alert');
 reason.required=true;reason.minLength=3;reason.maxLength=1000;reason.rows=3;reason.id='decision-reason';
 const reasonLabel=node('label','field','分诊或审批依据（必填）');reasonLabel.htmlFor=reason.id;
 for(const [value,label]of [['incident','普通故障'],['standard','标准服务请求'],['access','敏感：权限申请'],['security','敏感：安全例外'],['production','敏感：生产变更']]){const option=node('option','',label);option.value=value;category.append(option);}
 category.id='decision-category';const categoryLabel=node('label','field','分诊类型');categoryLabel.htmlFor=category.id;
 const routing=ticket.review_actions.some(name=>name==='triage'||name==='escalate');category.hidden=categoryLabel.hidden=!routing;
 if(ticket.review_actions.includes('escalate')){category.value='access';for(const option of category.options)if(['incident','standard'].includes(option.value))option.disabled=true;}
 const actions=node('div','actions'),labels={triage:'提交分诊',approve:'批准申请（不执行）',reject:'拒绝申请',escalate:'升级审批'};
 for(const name of ticket.review_actions){const button=node('button',name==='reject'?'secondary-button':'primary-button',labels[name]);button.type='submit';button.dataset.decision=name;actions.append(button);}
 form.append(node('p','field-help','申请人不能自审。请核对操作、资源和影响范围；批准只记录决策，不会执行操作。'),categoryLabel,category,reasonLabel,reason,error,actions);
 form.addEventListener('submit',async event=>{
  event.preventDefault();const selected=event.submitter?.dataset.decision;if(!selected||!form.reportValidity())return;
  const current=epoch,identity=$('identity').value,workspace=scope;for(const button of form.querySelectorAll('button'))button.disabled=true;$('identity').disabled=true;
  try{await api('/ticket-reviews/'+encodeURIComponent(ticket.ticket_id)+'/decisions',identity,workspace,{method:'POST',body:JSON.stringify({action:selected,version:ticket.workflow.version,reason:reason.value.trim(),...(['triage','escalate'].includes(selected)?{category:category.value}:{})})});if(current===epoch)await load();}
  catch(failure){if(current===epoch)error.textContent=failure.message+' 请刷新核对状态后重试。';}
  finally{$('identity').disabled=false;for(const button of form.querySelectorAll('button'))button.disabled=false;}
 });
 return form;
}
function activityForm(ticket){
 const status=ticket.workflow?.status,assigned=ticket.assignee===auth.user,owner=ticket.user_id===auth.user;
 const actions=[];
 if(owner){actions.push(['comment','补充留言']);if(status==='resolved')actions.push(['close','确认解决并关闭']);if(['resolved','closed'].includes(status))actions.push(['reopen','重新打开工单']);}
 else if(auth.ticketCapabilities.includes('handle')){if(['queued','approved'].includes(status))actions.push(['claim','领取并开始处理']);if(assigned){actions.push(['comment','添加处理留言']);if(status==='in_progress')actions.push(['resolve','记录解决结果']);}}
 if(!actions.length)return null;
 const form=node('form','ticket-decision'),label=node('label','field','处理说明 / 留言（必填）'),reason=node('textarea'),error=node('p','error-text'),buttons=node('div','actions');
 reason.id='activity-reason';reason.rows=3;reason.required=true;reason.minLength=3;reason.maxLength=2000;label.htmlFor=reason.id;error.setAttribute('role','alert');
 for(const [value,text]of actions){const b=node('button','secondary-button',text);b.type='submit';b.dataset.activity=value;buttons.append(b);}
 form.append(node('h3','','工单处理'),label,reason,error,buttons);
 form.onsubmit=async event=>{event.preventDefault();if(!form.reportValidity())return;const current=epoch,identity=$('identity').value;for(const b of form.querySelectorAll('button'))b.disabled=true;
 try{await api(`/tickets/${encodeURIComponent(ticket.ticket_id)}/activity`,identity,scope,{method:'POST',body:JSON.stringify({action:event.submitter.dataset.activity,version:ticket.workflow.version,reason:reason.value.trim()})});if(current===epoch)await load();}
 catch(failure){if(current===epoch)error.textContent=failure.message;}finally{for(const b of form.querySelectorAll('button'))b.disabled=false;}
 };return form;
}
async function notifications(identity,current){
 const result=await api('/notifications',identity);if(current!==epoch)return;const box=$('ticket-notifications');box.replaceChildren();
 const unread=result.items.filter(x=>!x.read_at);box.append(node('summary','',`站内通知 · ${unread.length} 条未读`));
 for(const item of result.items){const row=node('p'),link=node('a','',item.message+' · '+timeLabel(item.created_at));link.href='/workbench/tickets/'+encodeURIComponent(item.ticket_id)+(item.own_ticket?'':'?review=1');row.append(link);
 if(!item.read_at){const button=node('button','text-button','标为已读');button.type='button';button.onclick=async()=>{try{await api(`/notifications/${item.notification_id}/read`,identity,scope,{method:'POST'});await notifications(identity,current);}catch(error){$('ticket-notice').textContent=error.message;$('ticket-notice').hidden=false;}};row.append(button);}box.append(row);}
}

function renderList(data){
 $('page-title').textContent=reviewMode?'待我处理':'我的工单';
 if(!data.items.length){const box=node('section','panel ticket-empty');box.append(node('h2','',reviewMode?'暂无可处理工单':'暂无已提交工单'),node('p','',reviewMode?'只显示本租户、本人有能力处理的其他申请人的工单。':'在会话中让 Agent 整理工单，确认提交后即可在这里查看。'));const link=node('a','primary-button',reviewMode?'查看我的工单':'前往会话');link.href=reviewMode?'/workbench/tickets':'/';box.append(link);$('ticket-content').append(box);}
 else{const list=node('div','ticket-list');for(const t of data.items){const card=node('article','panel ticket-list-item'),link=ticketLink(t.ticket_id);if(reviewMode)link.href+='?review=1';card.append(node('span','ticket-badge waiting',ticketStatus(t)),node('h2','',t.title),node('p','field-help',(t.workflow?.category_label||'分类待核对')+(reviewMode?' · 申请人 '+t.user_id:'')),node('p','field-help',timeLabel(t.created_at)),node('p','ticket-reference',t.ticket_id),link);list.append(card);}$('ticket-content').append(list);}
 const prev=action('上一页',()=>{offset=Math.max(0,offset-30);load();}),next=action('下一页',()=>{offset+=30;load();});
 prev.disabled=offset===0;next.disabled=offset+data.items.length>=data.total;
 $('ticket-pagination').append(prev,node('span','field-help','共 '+data.total+' 条 · 第 '+(Math.floor(offset/30)+1)+' 页'),next);
}
async function load(){
 const current=++epoch,identity=$('identity').value;scope=null;$('ticket-content').replaceChildren(node('p','field-help','正在读取工单…'));$('ticket-pagination').replaceChildren();$('ticket-notifications').replaceChildren();$('ticket-notice').hidden=true;$('refresh-tickets').disabled=true;
 if($('ticket-source').open)$('ticket-source').close();
 try{
  const config=await api('/workbench/datasets/runtime',identity,null);if(current!==epoch)return;scope=config.fingerprint;
  $('review-tickets').hidden=!auth.ticketCapabilities.some(scope=>['triage','access_review','security_review','production_review'].includes(scope));
  await notifications(identity,current);if(current!==epoch)return;
  const prefix=reviewMode?'/ticket-reviews':'/tickets',id=location.pathname.split('/').filter(Boolean)[2],data=await api(id?prefix+'/'+encodeURIComponent(decodeURIComponent(id)):prefix+'?offset='+offset+'&limit=30',identity);
  if(current!==epoch)return;$('ticket-content').replaceChildren();id?renderDetail(data):renderList(data);window.dispatchEvent(new Event('knowledgeops:ticket-queue-changed'));
 }catch(e){if(current!==epoch)return;$('ticket-content').replaceChildren();$('ticket-notice').textContent=e.name==='AbortError'?'读取超时，请重试。':e.message;$('ticket-notice').hidden=false;}
 finally{if(current===epoch)$('refresh-tickets').disabled=false;}
}
$('refresh-tickets').addEventListener('click',load);
$('own-tickets').addEventListener('click',()=>location.assign('/workbench/tickets'));
$('review-tickets').addEventListener('click',()=>location.assign('/workbench/tickets?review=1'));
$('close-source').addEventListener('click',()=>$('ticket-source').close());
$('identity').addEventListener('change',()=>{try{sessionStorage.setItem('knowledgeops-identity',$('identity').value);}catch{}offset=0;load();});
load();
