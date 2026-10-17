import {DocumentQueue} from './knowledge-queue.js';
const $=id=>document.getElementById(id);
try{const identity=sessionStorage.getItem('knowledgeops-identity');if([...$('identity').options].some(o=>o.value===identity))$('identity').value=identity;}catch{}
const settingsPage=location.pathname==='/settings';
let documents=[],selected=null,collections=[],busy=false,previewConfig=null;
let library=null,queue=null,queueContext=null;
const selectedDocuments=new Set();
const labels={u_engineer_001:'工程师',u_employee_001:'普通员工',u_support_001:'IT 支持'};
function connectionState(name,text,state='saved'){
  const badge=document.getElementById(name+'-connection-state');if(!badge)return;badge.textContent=text;badge.dataset.state=state;
}
if(settingsPage){
  for(const name of ['database','embedding','rerank','generation']){
    const badge=document.createElement('span');badge.id=name+'-connection-state';badge.className='connection-status';badge.textContent='读取配置中';badge.setAttribute('role','status');$(name+'-section').querySelector('.section-heading').append(badge);
    $(name+'-section').addEventListener('input',()=>connectionState(name,'有未保存修改','dirty'));
    $(name+'-section').addEventListener('change',()=>connectionState(name,'有未保存修改','dirty'));
  }
}
function savedConnectionStates(connections,model){
  connectionState('database','已载入配置');
  connectionState('embedding',connections.embedding.configured?'已保存':'未配置',connections.embedding.configured?'saved':'dirty');
  connectionState('rerank',connections.rerank.configured?'已保存':'未启用');
  connectionState('generation',model.configured?'已保存':'未配置',model.configured?'saved':'dirty');
}
function node(tag,text,className=''){const n=document.createElement(tag);n.textContent=text;n.className=className;return n;}
function notice(text,error=false){$('notice').hidden=!text;$('notice').textContent=text;$('notice').classList.toggle('error',error);}
async function api(path,body,method=body?'POST':'GET'){
  const headers={'X-Local-User-Id':queueContext?.identity||$('identity').value};
  if(queueContext)headers['X-Knowledge-Workspace']=queueContext.fingerprint;let payload=body;
  if(body&&!(body instanceof FormData)){headers['Content-Type']='application/json';payload=JSON.stringify(body);}
  const response=await fetch(path,{method,headers,body:payload});const result=await response.json();
  const tested=path.match(/^\/workbench\/connections\/(database|embedding|rerank)\/test$/)?.[1]||(path==='/workbench/model/test'?'generation':null);
  if(!response.ok){if(tested)connectionState(tested,'检测失败','error');const error=new Error(result.error?.message||`请求失败 (${response.status})`);error.code=result.error?.code;throw error;}
  if(tested){const unsaved=document.getElementById(tested+'-connection-state')?.dataset.state==='dirty';connectionState(tested,unsaved?'检测通过 · 未保存':'检测通过',unsaved?'dirty':'saved');}
  if(path==='/workbench/model'&&method==='PUT')connectionState('generation','已保存');
  return result;
}
async function action(fn){if(busy)return;busy=true;notice('');for(const b of document.querySelectorAll('button'))b.disabled=true;$('identity').disabled=true;
  try{await fn();}catch(error){notice(error.message,true);}finally{busy=false;for(const b of document.querySelectorAll('button'))b.disabled=false;$('identity').disabled=false;syncControls();}}
function syncControls(){syncBulkSelection();const enabled=$('rerank-enabled').checked;for(const input of $('rerank-fields').querySelectorAll('input')){input.disabled=!enabled;input.required=enabled&&input.type!=='password';}$('test-rerank').disabled=!enabled;$('build').disabled=busy||!selected?.owned||!previewConfig||selected.status!=='succeeded';$('build').textContent=selected?.indexed_chunks?'重建索引':'构建索引';$('permissions-panel').hidden=!selected?.owned;}
function database(){return {host:$('db-host').value,port:Number($('db-port').value),database:$('db-database').value,user:$('db-user').value,password:$('db-password').value||null,schema_name:$('db-schema').value};}
function provider(name){return {base_url:$(name+'-url').value.trim(),model_id:$(name+'-model').value.trim(),api_key:$(name+'-key').value||null};}
function embedding(){return {...provider('embedding'),dimensions:Number($('embedding-dimensions').value),collection:$('embedding-collection').value.trim(),cost_per_million_tokens:$('embedding-price').value.trim()===''?null:Number($('embedding-price').value)};}
function chunkConfig(){return {strategy:$('chunk-strategy').value,size:Number($('chunk-size').value),overlap:Number($('chunk-overlap').value)};}
async function readiness(){const r=await api('/workbench/readiness');const box=$('readiness');box.replaceChildren();box.dataset.ready=String(r.ready);box.append(node('strong',r.ready?'知识库已就绪':'需要完成配置'),node('span',`${r.documents} 份可见文档 · ${r.indexed_documents} 份已索引 · ${r.indexed_chunks} 个切块`),node('span',`${r.schema} / ${r.collection}`));if(r.reasons.length)box.append(node('span',r.reasons.join('；')));const link=node('a',settingsPage?'查看知识库 →':'检查连接 →');link.href=settingsPage?'/knowledge':'/settings';box.append(link);}
async function loadSettings(){const c=await api('/workbench/connections');for(const [id,key] of [['host','host'],['port','port'],['database','database'],['user','user'],['schema','schema_name']])$('db-'+id).value=c.database[key];for(const name of ['embedding','rerank']){const p=c[name];$(name+'-url').value=p.base_url;$(name+'-model').value=p.model_id;$(name+'-key').placeholder=p.key_present?'已配置，留空沿用':'输入 API Key';} $('embedding-price').value=c.embedding.cost_per_million_tokens??'';$('embedding-status').textContent=c.embedding.configured&&c.embedding.cost_per_million_tokens==null?'尚未填写单价：知识问答可用，Agent 查阅知识库前需补齐。':'';$('embedding-dimensions').value=c.embedding.dimensions;$('embedding-collection').value=c.embedding.configured?c.embedding.collection:'';$('rerank-enabled').checked=c.rerank.configured;
  const g=await api('/workbench/model');$('generation-url').value=g.base_url;$('generation-model').value=g.model_id;$('generation-key').placeholder=g.key_present?'已配置，留空沿用':'输入 API Key';$('generation-input-price').value=g.agent_ready?g.input_price??'':'';$('generation-output-price').value=g.agent_ready?g.output_price??'':'';savedConnectionStates(c,g);syncControls();await detectDatabase();if(!c.embedding.configured){const existing=collections.find(item=>item.signature.provider==='compatible');if(existing){$('existing-collection').value=existing.collection;chooseCollection();}}}
async function detectDatabase(){const r=await api('/workbench/connections/database/test',database());collections=r.collections;$('schema-options').replaceChildren(...r.schemas.map(s=>{const n=document.createElement('option');n.value=s;return n;}));$('existing-collection').replaceChildren(new Option('填写新集合，或选择已有集合',''),...collections.filter(c=>c.signature.provider==='compatible').map(c=>new Option(`${c.collection} · ${c.signature.model_id} · ${c.dimensions} 维`,c.collection)));$('database-status').textContent=`连接正常 · ${r.collections.filter(c=>c.signature.provider==='compatible').length} 个真实模型集合`;}
function chooseCollection(){const c=collections.find(c=>c.collection===$('existing-collection').value);if(!c)return;if($('embedding-model').value!==c.signature.model_id||$('embedding-url').value.replace(/\/$/,'')!==c.signature.endpoint.replace(/\/$/,''))$('embedding-price').value='';$('embedding-collection').value=c.collection;$('embedding-dimensions').value=c.dimensions;$('embedding-model').value=c.signature.model_id;$('embedding-url').value=c.signature.endpoint;$('embedding-key').value='';$('embedding-status').textContent='已填入集合记录的模型签名，请填写密钥并测试。';}
async function loadDocuments(){library=await api('/workbench/knowledge');documents=library.documents;for(const id of selectedDocuments)if(!documents.some(d=>d.document_id===id&&d.owned&&d.status==='succeeded'))selectedDocuments.delete(id);$('document-count').textContent=`/ ${documents.length}`;if(selected){selected=documents.find(d=>d.document_id===selected.document_id)||null;if(!selected){previewConfig=null;$('document-detail').hidden=true;$('select-document').hidden=false;$('parsed-text').textContent='';$('chunk-preview').replaceChildren();}}renderDocuments();syncControls();}
function renderDocuments(){const query=$('document-search').value.toLowerCase();const rows=documents.filter(d=>[d.filename,d.title,d.source].some(v=>v?.toLowerCase().includes(query)));const list=$('document-list');list.replaceChildren();if(!rows.length){list.append(node('div',query?'没有匹配文档。':'当前身份没有可见文档。可先检查 schema 与权限，或上传新文档。','empty-state'));return;}for(const d of rows){const b=node('button','','document-item');b.type='button';b.disabled=busy;b.setAttribute('aria-pressed',String(selected?.document_id===d.document_id));b.append(node('strong',d.title||d.filename),node('small',`${d.filename} · ${d.version}`),node('small',d.source));let status=d.status==='succeeded'?(d.indexed_chunks?`已索引 · ${d.indexed_chunks} 个切块`:d.chunks?'待索引':'待切分'):{queued:'等待解析',processing:'解析中',failed:'解析失败'}[d.status]||d.status;b.append(node('small',status+(d.owned?' · 我上传的':''),'doc-status'));b.addEventListener('click',()=>action(()=>selectDocument(d)));const row=node('div','','document-row'),choice=node('label','','document-select'),checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.dataset.documentId=d.document_id;checkbox.checked=selectedDocuments.has(d.document_id);checkbox.disabled=busy||!d.owned||d.status!=='succeeded';checkbox.setAttribute('aria-label',`选择 ${d.title||d.filename}`);checkbox.onchange=()=>{if(checkbox.checked)selectedDocuments.add(d.document_id);else selectedDocuments.delete(d.document_id);syncBulkSelection();};choice.append(checkbox);row.append(choice,b);list.append(row);}syncBulkSelection();}
async function selectDocument(d){selected=d;previewConfig=null;$('document-detail').hidden=false;$('select-document').hidden=true;$('document-title').textContent=d.title||d.filename;$('document-meta').textContent=`${d.source} · ${d.version} · ${d.document_id}`;$('preview-summary').textContent='';$('parsed-text').textContent='';$('chunk-preview').replaceChildren();$('permissions').replaceChildren();for(const [id,label] of Object.entries(labels)){const input=document.createElement('input');input.type='checkbox';input.value=id;input.checked=(d.user_ids||[]).includes(id);const lab=node('label',label);lab.prepend(input);$('permissions').append(lab);}renderDocuments();syncControls();if(d.status==='succeeded')await preview();else{ $('preview-summary').textContent=d.status==='failed'?`解析失败：${d.error_code}。可重试解析。`:'文档还在解析，请稍后刷新。';if(d.status==='failed'){const retry=node('button','重试解析','secondary-button');retry.type='button';retry.onclick=()=>action(async()=>{await api(`/ingestion/tasks/${encodeURIComponent(d.task_id)}/retry`,{});await loadDocuments();notice('已提交解析重试，请刷新查看状态。');});$('chunk-preview').append(retry);}}}
async function preview(){if(!selected||!$('chunk-form').reportValidity())return;const config=chunkConfig();if(config.overlap>=config.size)throw new Error('重叠字符数必须小于切块大小。');const r=await api(`/workbench/knowledge/${encodeURIComponent(selected.document_id)}/preview`,config);previewConfig=config;$('parsed-text').textContent=r.text;$('preview-summary').textContent=`预览 ${r.count} 个切块 · 原文 ${r.text.length} 字符${r.truncated?' · 仅展示前 100 块':''}`;$('chunk-preview').replaceChildren(...r.chunks.map((c,i)=>{const box=node('article','','chunk-card');box.append(node('small',`${String(i+1).padStart(2,'0')} / 字符 ${c.start_char}–${c.end_char} / ${(c.heading_paths||[]).join(' › ')}`),node('p',c.text));return box;}));syncControls();}
async function refresh(){await readiness();if(settingsPage)await loadSettings();else await loadDocuments();}
$('knowledge-page').hidden=settingsPage;$('settings-page').hidden=!settingsPage;document.querySelector(`.workspace-nav a[href="${location.pathname}"]`)?.setAttribute('aria-current','page');if(settingsPage){$('page-title').textContent='连接设置';$('page-subtitle').textContent='管理数据库和模型服务。';}
$('refresh').onclick=()=>action(refresh);$('identity').onchange=()=>{try{sessionStorage.setItem('knowledgeops-identity',$('identity').value);}catch{}selectedDocuments.clear();queue=null;$('knowledge-progress').hidden=true;selected=null;previewConfig=null;$('document-detail').hidden=true;$('select-document').hidden=false;action(refresh);};$('document-search').oninput=renderDocuments;$('rerank-enabled').onchange=syncControls;$('existing-collection').onchange=chooseCollection;
$('test-database').onclick=()=>action(detectDatabase);
for(const name of ['embedding','rerank'])$("test-"+name).onclick=()=>action(async()=>{await api(`/workbench/connections/${name}/test`,name==='embedding'?embedding():provider(name));$(name+'-status').textContent='测试通过';});
$('connections-form').onsubmit=e=>{e.preventDefault();action(async()=>{await api('/workbench/connections',{database:database(),embedding:embedding(),rerank:$('rerank-enabled').checked?provider('rerank'):null},'PUT');for(const id of ['db-password','embedding-key','rerank-key'])$(id).value='';notice('连接已应用。返回工作台后请新建会话，再运行评测。');await readiness();await loadSettings();});};
function generation(){const g=provider('generation');const a=$('generation-input-price').value,b=$('generation-output-price').value;if(a!==''||b!==''){if(a===''||b==='')throw new Error('请同时填写输入与输出单价。');g.input_price=Number(a);g.output_price=Number(b);}return g;}
$('test-generation').onclick=()=>action(async()=>{await api('/workbench/model/test',generation());$('generation-status').textContent='连接与 JSON 输出测试通过';});$('generation-form').onsubmit=e=>{e.preventDefault();action(async()=>{await api('/workbench/model',generation(),'PUT');$('generation-key').value='';$('generation-status').textContent='模型已保存';await readiness();});};
$('chunk-form').onsubmit=e=>{e.preventDefault();action(preview);};$('chunk-form').oninput=()=>{previewConfig=null;syncControls();};$('build').onclick=()=>action(async()=>{if(!previewConfig)return;$('preview-summary').textContent='正在切分、计算向量并写入索引…';try{const r=await api(`/workbench/knowledge/${encodeURIComponent(selected.document_id)}/build`,{...previewConfig,force:true});$('preview-summary').textContent=`索引完成 · ${r.count} 个切块 · ${Math.round(r.duration_ms)} ms`;notice('文档索引已完成，可以返回会话进行检索。');}finally{await loadDocuments();await readiness();}});
for(const view of ['original','chunks'])$('show-document-'+view).onclick=()=>{
  $('original-preview').hidden=view!=='original';$('chunk-preview').hidden=view!=='chunks';
  for(const name of ['original','chunks'])$('show-document-'+name).setAttribute('aria-pressed',String(name===view));
};
$('save-permissions').onclick=()=>action(async()=>{const ids=[...$('permissions').querySelectorAll('input:checked')].map(n=>n.value);await api(`/documents/${encodeURIComponent(selected.document_id)}/permissions`,{user_ids:ids},'PUT');notice('权限已保存。');await loadDocuments();await readiness();});

function visibleOwnedDocuments(){const query=$('document-search').value.toLowerCase();return documents.filter(d=>d.owned&&d.status==='succeeded'&&[d.filename,d.title,d.source].some(v=>v?.toLowerCase().includes(query)));}
function syncBulkSelection(){
  for(const input of document.querySelectorAll('.document-select input')){const d=documents.find(d=>d.document_id===input.dataset.documentId);input.disabled=busy||!d?.owned||d.status!=='succeeded';}
  const rows=visibleOwnedDocuments(),count=rows.filter(d=>selectedDocuments.has(d.document_id)).length;
  $('selection-count').textContent=`已选 ${selectedDocuments.size} 份`;
  $('select-visible').checked=!!rows.length&&count===rows.length;
  $('select-visible').indeterminate=count>0&&count<rows.length;
  $('select-visible').disabled=busy||!rows.length;
  $('open-bulk-build').disabled=busy||!selectedDocuments.size;
}
$('select-visible').onchange=()=>{for(const d of visibleOwnedDocuments()){if($('select-visible').checked)selectedDocuments.add(d.document_id);else selectedDocuments.delete(d.document_id);}renderDocuments();};
function renderQueue(){
  if(!queue)return;
  const names={pending:'待处理',running:'处理中',succeeded:'成功',failed:'失败',skipped:'已跳过'};
  const counts=Object.fromEntries(Object.keys(names).map(k=>[k,queue.items.filter(x=>x.status===k).length]));
  $('knowledge-progress').hidden=false;
  $('queue-title').textContent=queue.kind==='upload'?'批量上传与解析':'批量构建索引';
  $('queue-summary').textContent=`${queue.running?(queue.stopRequested?'等待当前文档完成后停止':'处理中'):counts.pending?'已停止':'处理结束'} · 共 ${queue.items.length} 份 · 成功 ${counts.succeeded} · 跳过 ${counts.skipped} · 失败 ${counts.failed} · 未处理 ${counts.pending}`;
  $('stop-queue').hidden=!queue.running;$('stop-queue').disabled=queue.stopRequested;
  $('retry-queue').hidden=queue.running||!counts.failed;$('retry-queue').disabled=queue.running;
  $('continue-queue').hidden=queue.running||!counts.pending;$('continue-queue').disabled=queue.running;
  $('queue-items').replaceChildren(...queue.items.map(item=>{const row=node('li','');row.dataset.status=item.status;row.append(node('span',item.name),node('span',names[item.status]),node('small',item.detail||'等待处理'));return row;}));
}
async function runQueue(mode='pending'){
  if(busy||!queue)return;
  busy=true;queueContext=queue.context;notice('');
  const controls=[...document.querySelectorAll('button,input,select,textarea')];
  const disabled=controls.map(control=>control.disabled);for(const control of controls)control.disabled=true;
  $('stop-queue').disabled=false;
  try{await queue.run(mode);}finally{
    queueContext=null;busy=false;
    controls.forEach((control,i)=>control.disabled=disabled[i]);
    try{await loadDocuments();await readiness();}catch(error){notice('列表刷新失败：'+error.message,true);}
    syncControls();renderQueue();
  }
}
function installQueue(kind,items,work){
  queue=new DocumentQueue(items,work,renderQueue);queue.kind=kind;
  queue.context={identity:$('identity').value,fingerprint:library.runtime_fingerprint};
  renderQueue();$('knowledge-progress').scrollIntoView({block:'start'});return runQueue();
}
$('stop-queue').onclick=()=>queue?.stop();
$('retry-queue').onclick=()=>runQueue('failed');
$('continue-queue').onclick=()=>runQueue();
window.addEventListener('beforeunload',event=>{if(queue?.running){event.preventDefault();event.returnValue='';}});
for(const link of document.querySelectorAll('.workspace-nav a,.brand'))link.addEventListener('click',event=>{if(queue?.running){event.preventDefault();notice('任务正在运行，请先停止后续任务，等待当前文档完成再离开。');}});
$('open-bulk-build').onclick=()=>{
  $('bulk-build-summary').textContent=`已选择 ${selectedDocuments.size} 份文档。使用当前 Embedding 连接与索引集合。`;
  $('bulk-build-error').textContent='';$('bulk-force').checked=false;$('bulk-build-dialog').showModal();
};
$('close-bulk-build').onclick=()=>$('bulk-build-dialog').close();
$('bulk-build-form').onsubmit=event=>{
  event.preventDefault();if(busy||!$('bulk-build-form').reportValidity())return;
  const config={strategy:$('bulk-strategy').value,size:Number($('bulk-size').value),overlap:Number($('bulk-overlap').value),force:$('bulk-force').checked};
  if(config.overlap>=config.size){$('bulk-build-error').textContent='重叠字符数必须小于切块大小。';return;}
  if(!library?.embedding_ready){$('bulk-build-error').textContent='请先在连接设置配置真实 Embedding，再刷新知识库。';return;}
  const items=documents.filter(d=>selectedDocuments.has(d.document_id)&&d.owned&&d.status==='succeeded').map(d=>({id:d.document_id,name:d.title||d.filename,status:'pending'}));
  if(!items.length)return;
  $('bulk-build-dialog').close();
  installQueue('build',items,async(item,progress)=>{
    progress(config.force?'正在重建：切分、Embedding 与写入…':'检查有效索引；缺失时切分、Embedding 与写入…');
    const r=await api(`/workbench/knowledge/${encodeURIComponent(item.id)}/build`,config);
    return {...r,message:r.reused?`已有有效索引 · ${r.count} 个切块`:`已索引 ${r.count} 个切块 · ${Math.round(r.duration_ms)} ms`};
  });
};
$('open-upload').onclick=()=>{$('upload-status').textContent='';$('upload-limits').textContent=`单文件上限 ${Math.round((library?.max_document_bytes||1048576)/1024)} KB；每批最多 100 份、合计 50 MB。`;$('upload-dialog').showModal();};
$('close-upload').onclick=()=>$('upload-dialog').close();
$('upload-file').onchange=()=>{$('upload-source').disabled=$('upload-file').files.length>1;};
$('upload-form').onsubmit=event=>{
  event.preventDefault();if(busy||!$('upload-form').reportValidity())return;
  const files=[...$('upload-file').files],limit=library?.max_document_bytes||1048576;
  if(!files.length||files.length>100||files.reduce((n,f)=>n+f.size,0)>50*1024*1024){$('upload-status').textContent='请选择 1–100 个文件，每批合计不超过 50 MB。';return;}
  if(!library?.runtime_fingerprint){$('upload-status').textContent='请先刷新知识库状态。';return;}
  const source=files.length===1?$('upload-source').value.trim():'',version=$('upload-version').value.trim();
  const items=files.map(file=>({name:file.name,file,status:'pending',receipt:null}));
  $('upload-dialog').close();
  installQueue('upload',items,async(item,progress)=>{
    if(!/\.(md|txt)$/i.test(item.name))throw new Error('仅支持 Markdown / TXT');
    if(!item.file.size||item.file.size>limit)throw new Error(`文件为空或超过单文件上限 ${Math.round(limit/1024)} KB`);
    if(!item.receipt){
      progress('正在上传…');const form=new FormData();form.append('file',item.file);if(source)form.append('source',source);if(version)form.append('version',version);
      item.receipt=await api('/documents',form);
    }
    progress('已上传，等待解析…');const path=`/ingestion/tasks/${encodeURIComponent(item.receipt.task_id)}`;
    let task=await api(path);
    if(task.status==='failed'){await api(path+'/retry',{});task={status:'queued'};}
    for(let i=0;i<120;i++){
      if(task.status==='succeeded')return {message:item.receipt.duplicate?'已复用相同文档；可勾选构建索引':'上传与解析完成；可勾选构建索引'};
      if(task.status==='failed')throw new Error('解析失败：'+task.error_code);
      await new Promise(resolve=>setTimeout(resolve,500));task=await api(path);
    }
    throw new Error('解析仍在进行；可稍后重试检查状态，不会重复上传。');
  });
};

action(refresh);
