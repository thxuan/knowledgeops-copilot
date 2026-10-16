import {node,action,timeLabel,ticketLink,progressView} from './agent-view.js';
const $=id=>document.getElementById(id);
let epoch=0,offset=0,scope=null;
try{const saved=sessionStorage.getItem('knowledgeops-identity');if([...$('identity').options].some(o=>o.value===saved))$('identity').value=saved;}catch{}
async function api(path,identity,workspace=scope){
 const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),20000);
 try{
  const res=await fetch(path,{signal:controller.signal,headers:{'X-Local-User-Id':identity,...(workspace?{'X-Knowledge-Workspace':workspace}:{})}});
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
 const back=node('a','quiet-link','← 返回我的工单');back.href='/workbench/tickets';
 const main=node('article','panel ticket-detail'),heading=node('div','ticket-card-heading');
 heading.append(node('span','eyebrow','本地模拟工单'),node('span','ticket-badge success','已创建'));
 main.append(heading,node('h2','',ticket.title));
 const meta=node('dl','ticket-meta');meta.append(field('工单编号',ticket.ticket_id),field('提交时间',timeLabel(ticket.created_at)),field('提交身份',ticket.user_id),field('已批准版本','版本 '+ticket.approval_version));
 main.append(meta,node('h3','','问题描述'),node('p','ticket-description',ticket.description),node('p','field-help','已创建，待分配。尚未接入派单和处理系统，暂无处理人或预计完成时间。'));
 const history=node('section','panel ticket-history');history.append(node('h2','','处理与审批记录'));
 const timeline=node('ol','ticket-timeline'),names={accepted:'收到处理任务',waiting_approval:'整理工单，等待确认',proposal_revised:'用户修改了工单内容',tool_end:'用户已批准，工单创建成功'};
 for(const e of ticket.timeline||[]){
  const li=node('li');li.append(node('strong','',names[e.type]||e.type),node('time','field-help',timeLabel(e.time)));
  if(e.version)li.append(node('p','field-help','提案版本 '+e.version));timeline.append(li);
 }
 history.append(timeline);
 if(ticket.run_status==='failed'||ticket.run_status==='interrupted')history.append(node('p','notice','工单已成功写入，Agent 后续回复'+(ticket.run_status==='failed'?'失败':'中断')+'；无需重复建单。'));
 const evidence=node('section','panel ticket-history');evidence.append(node('h2','','建单前的排障依据'),progressView({trace:ticket.trace||[],status:'completed'},source));
 $('ticket-content').append(back,main,history,evidence);
}
function renderList(data){
 $('page-title').textContent='我的工单';
 if(!data.items.length){const box=node('section','panel ticket-empty');box.append(node('h2','','暂无已创建工单'),node('p','','在会话中让 Agent 整理工单，确认创建后即可在这里查看。'));const link=node('a','primary-button','前往会话');link.href='/';box.append(link);$('ticket-content').append(box);}
 else{const list=node('div','ticket-list');for(const t of data.items){const card=node('article','panel ticket-list-item');card.append(node('span','ticket-badge success','已创建'),node('h2','',t.title),node('p','field-help',timeLabel(t.created_at)),node('p','ticket-reference',t.ticket_id),ticketLink(t.ticket_id));list.append(card);}$('ticket-content').append(list);}
 const prev=action('上一页',()=>{offset=Math.max(0,offset-30);load();}),next=action('下一页',()=>{offset+=30;load();});
 prev.disabled=offset===0;next.disabled=offset+data.items.length>=data.total;
 $('ticket-pagination').append(prev,node('span','field-help','共 '+data.total+' 条 · 第 '+(Math.floor(offset/30)+1)+' 页'),next);
}
async function load(){
 const current=++epoch,identity=$('identity').value;scope=null;$('ticket-content').replaceChildren(node('p','field-help','正在读取工单…'));$('ticket-pagination').replaceChildren();$('ticket-notice').hidden=true;$('refresh-tickets').disabled=true;
 if($('ticket-source').open)$('ticket-source').close();
 try{
  const config=await api('/workbench/datasets/runtime',identity,null);if(current!==epoch)return;scope=config.fingerprint;
  const id=location.pathname.split('/').filter(Boolean)[2],data=await api(id?'/tickets/'+encodeURIComponent(decodeURIComponent(id)):'/tickets?offset='+offset+'&limit=30',identity);
  if(current!==epoch)return;$('ticket-content').replaceChildren();id?renderDetail(data):renderList(data);
 }catch(e){if(current!==epoch)return;$('ticket-content').replaceChildren();$('ticket-notice').textContent=e.name==='AbortError'?'读取超时，请重试。':e.message;$('ticket-notice').hidden=false;}
 finally{if(current===epoch)$('refresh-tickets').disabled=false;}
}
$('refresh-tickets').addEventListener('click',load);
$('close-source').addEventListener('click',()=>$('ticket-source').close());
$('identity').addEventListener('change',()=>{try{sessionStorage.setItem('knowledgeops-identity',$('identity').value);}catch{}offset=0;load();});
load();
