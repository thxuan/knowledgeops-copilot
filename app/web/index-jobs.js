import {auth,authenticatedFetch as fetch} from './auth-session.js';
import {CompletedJobHistory} from './index-job-history.js';
const $=id=>document.getElementById(id),names={queued:'排队中',running:'处理中',succeeded:'已完成',failed:'失败',cancelled:'已取消',blocked:'配置待恢复'};
let storage=null;try{storage=localStorage;}catch{}
const history=new CompletedJobHistory(storage,auth.user);
let busy=false,lastCleanup=null;
async function api(path,options={}){const r=await fetch(path,{...options,headers:{'X-Local-User-Id':auth.user}});const data=await r.json();if(!r.ok)throw new Error(data.error?.message||'读取失败');return data;}
async function load({clearCompleted=false}={}){
 if(busy)return;busy=true;$('refresh-jobs').disabled=true;$('restore-jobs').disabled=true;
 try{
  const r=await api('/index-jobs?limit=100');
  if(clearCompleted)lastCleanup=history.clearCompleted(r.items);
  const jobs=history.visible(r.items);$('job-list').replaceChildren();
  if(!jobs.length)$('job-list').textContent=r.items.length?'暂无需要显示的任务。已完成记录已从列表移除，文档和索引均保留。':'暂无索引任务。从知识库构建索引后，可在这里查看。';
  for(const job of jobs){
   const card=document.createElement('article');card.className='panel job-card';card.dataset.jobId=job.job_id;
   const title=document.createElement('h2');title.textContent=job.title+(job.version?' · 草稿 '+job.version:' · 当前正文');
   const status=document.createElement('p');const stage=({queued:'等待处理',chunking:'正在切分',embedding:'正在向量化',completed:'已完成',succeeded:'已完成',failed:'处理失败',blocked:'等待恢复配置',cancelled:'已取消'})[job.stage]||job.stage;
   status.textContent=`${names[job.status]}${stage&&stage!==names[job.status]?' · '+stage:''} · 尝试 ${job.attempts}/3 · ${job.completed}/${job.total} 个切块`;
   const progress=document.createElement('progress');progress.max=Math.max(1,job.total);progress.value=job.completed;progress.setAttribute('aria-label','切块处理进度');
   const info=document.createElement('p');info.className='field-help';info.textContent=`${new Date(job.created_at*1000).toLocaleString('zh-CN')} · ${job.collection} · ${job.job_id}`;card.append(title,status,progress,info);
   if(job.version&&job.status==='succeeded'){const note=document.createElement('p');note.textContent='草稿索引已就绪，请返回知识库提交发布审批，由管理员通过后发布。';card.append(note);}
   if(job.error_code){const error=document.createElement('p');error.className='error-text';error.textContent=`${job.error_code}；旧版索引未被本次失败替换。`;card.append(error);}
   const button=(name,label)=>{const b=document.createElement('button');b.className='secondary-button';b.textContent=label;b.onclick=async()=>{b.disabled=true;try{await api(`/index-jobs/${job.job_id}/${name}`,{method:'POST'});await load();}catch(error){$('job-message').textContent=error.message;}finally{b.disabled=false;}};card.append(b);};
   if(['queued','running','blocked'].includes(job.status))button('cancel','取消任务');
   if(['failed','cancelled','blocked'].includes(job.status)&&job.attempts<3)button('retry','重试（使用当前模型配置）');
   $('job-list').append(card);
  }
  const scope=auth.canManage?'平台管理员可查看当前组织、当前知识空间内的任务。':'仅显示本人提交、仍有文档管理权的任务。';
  $('job-message').textContent=scope+'刷新任务会移除当前账号列表中成功完成的记录，保留失败及待处理任务。'+(lastCleanup===null?'':`本次已移除 ${lastCleanup} 条已完成记录。`);
  $('restore-jobs').hidden=!history.dismissed.size;
 }catch(error){$('job-message').textContent=error.message;}
 finally{busy=false;$('refresh-jobs').disabled=false;$('restore-jobs').disabled=false;}
}
$('refresh-jobs').onclick=()=>load({clearCompleted:true});
$('restore-jobs').onclick=()=>{if(busy)return;history.restore();lastCleanup=null;load();};
setInterval(()=>{if(!document.hidden)load();},3000);load();
