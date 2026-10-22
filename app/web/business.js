import {auth,authenticatedFetch as fetch} from './auth-session.js';
import {node,timeLabel,ticketLink,ticketStatus} from './agent-view.js?v=20261017-ticket-policy';
const $=id=>document.getElementById(id);
let epoch=0;
try{const id=sessionStorage.getItem('knowledgeops-identity');if([...$('identity').options].some(o=>o.value===id))$('identity').value=id;}catch{}
const yesNo=value=>value===true?'是':value===false?'否':'未知';
function metric(label,value){const item=node('div','business-metric');item.append(node('dt','',label),node('dd','',value??'未记录'));return item;}
function link(label,task){const a=node('a','secondary-button',label);a.href='/?business_task='+encodeURIComponent(task);return a;}
function card(title,section){
 const box=node('section','panel business-card');box.append(node('h2','',title));
 if(section?.error||!section?.data){box.append(node('p','field-help',section?.error?.message||'暂无记录'));return [box,null];}
 return [box,section.data];
}
function stamp(box,section,d){
 box.append(node('p','field-help','记录时间：'+(d.observed_at?new Date(d.observed_at).toLocaleString('zh-CN'):'未记录')+' · 本次读取：'+timeLabel(section.queried_at)));
}
function render(sections){
 const grid=node('div','business-grid');
 const [device,d]=card('设备与合规',sections.device_status);
 if(d){
  device.append(node('span','ticket-badge '+(d.compliant===true?'success':'waiting'),d.compliant===true?'合规':d.compliant===false?'需处理':'合规状态未知'));
  const fields=node('dl','business-fields');
  fields.append(metric('资产编号',d.asset_id),metric('操作系统',d.os),metric('受管设备',yesNo(d.managed)),
    metric('补丁状态',({pending:'待更新',up_to_date:'已更新',unknown:'未记录'})[d.patch_status]||'未知'),
    metric('EDR 状态',({online:'在线',offline:'离线'})[d.edr_status]||'未知'),metric('磁盘加密',yesNo(d.disk_encrypted)));
  device.append(fields,node('p','',d.reason||'未记录具体原因'));stamp(device,sections.device_status,d);
  device.append(link('让 Agent 分析设备问题','device'));
 }
 const [account,a]=card('账号与安全',sections.account_status);
 if(a){
  account.append(node('span','ticket-badge '+(a.account_enabled===true?'success':'waiting'),a.account_enabled===true?'账号已启用':a.account_enabled===false?'账号已停用':'账号状态未知'));
  const fields=node('dl','business-fields');
  fields.append(metric('身份系统',a.system),metric('已登记 MFA 方法',Array.isArray(a.mfa_methods)?a.mfa_methods.join('、'):'未记录'),
   metric('MFA 方法数',a.mfa_methods_count),metric('临时 PRD 权限',yesNo(a.temporary_prd_access)),
   metric('PRD 权限到期',a.temporary_prd_access?(a.temporary_prd_expires_at||'未记录'):'不适用'));
  account.append(fields);const access=node('ul','business-access');
  for(const p of a.access||[])access.append(node('li','',(p.resource||'未知资源')+' · '+(p.access==='standard'?'标准访问':p.access||'未知权限')+' · '+(p.expires_at?'到期 '+p.expires_at:'未记录到期时间')));
  account.append(access);stamp(account,sections.account_status,a);
  account.append(link('让 Agent 核对账号安全','account'));
 }
 grid.append(device,account);
 const [tickets,t]=card('工单进度',sections.ticket_status);
 if(t){
  tickets.append(node('p','field-help','本人共 '+t.total+' 条已提交工单，展示最近 '+t.items.length+' 条。'));
  if(!t.items.length)tickets.append(node('p','business-empty','暂无已提交工单。可以先让 Agent 分析设备或账号问题，再整理提案供你确认。'));
  for(const item of t.items){
   const row=node('article','business-ticket');row.append(node('strong','',item.title),node('span','ticket-badge waiting',ticketStatus(item)));
   row.append(node('p','field-help','处理人：'+(item.assignee||'未分配')+' · 预计完成：'+(item.estimated_completion||'未提供')),node('p','field-help','提交时间：'+timeLabel(item.created_at)),ticketLink(item.ticket_id));
   tickets.append(row);
  }
  tickets.append(node('p','field-help',t.note));
  const actions=node('div','actions');const all=node('a','secondary-button','查看全部工单');all.href='/workbench/tickets';actions.append(all,link('让 Agent 查询工单进度','tickets'));tickets.append(actions);
 }
 const duration=sections.ticket_durations;
 const demo=node('details','business-duration');demo.append(node('summary','','独立演示：工单耗时计算'));
 if(duration?.data){
  const v=duration.data;demo.append(node('p','field-help',v.note||'独立耗时样例，不对应上方已创建工单。'));
  demo.append(node('p','','分诊 '+v.triage_minutes+' 分钟 · 处理 '+v.resolution_minutes+' 分钟 · 目标 '+v.target_minutes+' 分钟'),link('让 Agent 计算是否超时','durations'));
 }else demo.append(node('p','field-help','暂无耗时演示记录。'));
 $('business-content').append(grid,tickets,demo);
}
async function read(path,identity,scope){
 const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),20000);
 try{const r=await fetch(path,{signal:controller.signal,headers:{'X-Local-User-Id':identity,...(scope?{'X-Knowledge-Workspace':scope}:{})}});const data=await r.json();if(!r.ok)throw new Error(data.error?.message||'业务记录读取失败');return data;}finally{clearTimeout(timer);}
}
async function load(){
 const current=++epoch,id=$('identity').value;
 $('business-content').replaceChildren(node('p','field-help','正在读取本人业务记录…'));$('business-notice').hidden=true;$('refresh-business').disabled=true;
 try{
  const config=await read('/workbench/datasets/runtime',id);if(current!==epoch)return;
  const result=await read('/business/overview',id,config.fingerprint);if(current!==epoch)return;
  $('business-content').replaceChildren();render(result.sections);
 }catch(e){if(current===epoch){$('business-content').replaceChildren();$('business-notice').hidden=false;$('business-notice').textContent=e.name==='AbortError'?'读取超时，请重试。':e.message;}}
 finally{if(current===epoch)$('refresh-business').disabled=false;}
}
$('refresh-business').addEventListener('click',load);
$('identity').addEventListener('change',()=>{try{sessionStorage.setItem('knowledgeops-identity',$('identity').value);}catch{}load();});
load();
