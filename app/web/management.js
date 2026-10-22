import {auth,authenticatedFetch as fetch} from './auth-session.js';
import {documentLifecycle} from './document-lifecycle.js';
import {DocumentQueue} from './knowledge-queue.js';
import {installBulkReview} from './bulk-review.js';
import {installBulkApproval} from './bulk-approval.js';
const $=id=>document.getElementById(id);
try{const identity=sessionStorage.getItem('knowledgeops-identity');if([...$('identity').options].some(o=>o.value===identity))$('identity').value=identity;}catch{}
const settingsPage=location.pathname==='/settings';
let documents=[],selected=null,collections=[],busy=false,previewConfig=null;
let documentGroup="existing";
let library=null,queue=null,queueContext=null;
const lifecycle=documentLifecycle({api,action,chunkConfig,labels:()=>labels,reload:async()=>{await loadDocuments();await loadReviews();await readiness();if(selected)await selectDocument(selected);}});
const selectedDocuments=new Set();
const labels={u_engineer_001:'工程师',u_employee_001:'普通员工',u_support_001:'IT 支持'};
const bulkReview=installBulkReview({api,action,selection:()=>documents.filter(d=>selectedDocuments.has(d.document_id)),context:()=>({identity:$('identity').value,fingerprint:library?.runtime_fingerprint}),labels:()=>labels,start:(items,work,context)=>installQueue('review',items,work,context),isBusy:()=>busy});
const bulkApproval=installBulkApproval({api,canApprove:()=>auth.canManage,context:()=>({identity:$('identity').value,fingerprint:library?.runtime_fingerprint}),labels:()=>labels,start:(items,work,context)=>installQueue('approve',items,work,context),isBusy:()=>busy,notify:notice});
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
async function api(path,body,method=body?'POST':'GET',context=queueContext){
  const headers={'X-Local-User-Id':context?.identity||$('identity').value};
  if(context)headers['X-Knowledge-Workspace']=context.fingerprint;let payload=body;
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
function syncControls(){syncBulkSelection();bulkReview.sync(busy);bulkApproval.sync(busy);const enabled=$('rerank-enabled').checked;for(const input of $('rerank-fields').querySelectorAll('input')){input.disabled=!enabled;input.required=enabled&&input.type!=='password';}$('test-rerank').disabled=!enabled;$('build').disabled=busy||!selected?.can_manage||!previewConfig||selected.status!=='succeeded';$('build').textContent=selected?.indexed_chunks?'重建索引':'构建索引';lifecycle.sync(busy);$('build').disabled||=!!selected?.withdrawn;}
function database(){return {host:$('db-host').value,port:Number($('db-port').value),database:$('db-database').value,user:$('db-user').value,password:$('db-password').value||null,schema_name:$('db-schema').value};}
function provider(name){return {base_url:$(name+'-url').value.trim(),model_id:$(name+'-model').value.trim(),api_key:$(name+'-key').value||null};}
function embedding(){return {...provider('embedding'),dimensions:Number($('embedding-dimensions').value),collection:$('embedding-collection').value.trim(),cost_per_million_tokens:$('embedding-price').value.trim()===''?null:Number($('embedding-price').value)};}
function chunkConfig(){return {strategy:$('chunk-strategy').value,size:Number($('chunk-size').value),overlap:Number($('chunk-overlap').value)};}
async function readiness(){const r=await api('/workbench/readiness');const box=$('readiness');box.replaceChildren();box.dataset.ready=String(r.ready);box.append(node('strong',r.ready?'知识库已就绪':'需要完成配置'),node('span',`${r.documents} 份可见文档 · ${r.indexed_documents} 份已索引 · ${r.indexed_chunks} 个切块`),node('span',`${r.schema} / ${r.collection}`));if(r.reasons.length)box.append(node('span',r.reasons.join('；')));const link=node(auth.canManage?'a':'span',auth.canManage?(settingsPage?'查看知识库 →':'检查连接 →'):'请联系平台管理员完成配置');if(auth.canManage)link.href=settingsPage?'/knowledge':'/settings';box.append(link);}
async function loadSettings(){const c=await api('/workbench/connections');for(const [id,key] of [['host','host'],['port','port'],['database','database'],['user','user'],['schema','schema_name']])$('db-'+id).value=c.database[key];for(const name of ['embedding','rerank']){const p=c[name];$(name+'-url').value=p.base_url;$(name+'-model').value=p.model_id;$(name+'-key').placeholder=p.key_present?'已配置，留空沿用':'输入 API Key';} $('embedding-price').value=c.embedding.cost_per_million_tokens??'';$('embedding-status').textContent=c.embedding.configured&&c.embedding.cost_per_million_tokens==null?'尚未填写单价：知识问答可用，Agent 查阅知识库前需补齐。':'';$('embedding-dimensions').value=c.embedding.dimensions;$('embedding-collection').value=c.embedding.configured?c.embedding.collection:'';$('rerank-enabled').checked=c.rerank.configured;
  const g=await api('/workbench/model');$('generation-url').value=g.base_url;$('generation-model').value=g.model_id;$('generation-key').placeholder=g.key_present?'已配置，留空沿用':'输入 API Key';$('generation-input-price').value=g.agent_ready?g.input_price??'':'';$('generation-output-price').value=g.agent_ready?g.output_price??'':'';savedConnectionStates(c,g);syncControls();await detectDatabase();if(!c.embedding.configured){const existing=collections.find(item=>item.signature.provider==='compatible');if(existing){$('existing-collection').value=existing.collection;chooseCollection();}}}
async function detectDatabase(){const r=await api('/workbench/connections/database/test',database());collections=r.collections;$('schema-options').replaceChildren(...r.schemas.map(s=>{const n=document.createElement('option');n.value=s;return n;}));$('existing-collection').replaceChildren(new Option('填写新集合，或选择已有集合',''),...collections.filter(c=>c.signature.provider==='compatible').map(c=>new Option(`${c.collection} · ${c.signature.model_id} · ${c.dimensions} 维`,c.collection)));$('database-status').textContent=`连接正常 · ${r.collections.filter(c=>c.signature.provider==='compatible').length} 个真实模型集合`;}
function chooseCollection(){const c=collections.find(c=>c.collection===$('existing-collection').value);if(!c)return;if($('embedding-model').value!==c.signature.model_id||$('embedding-url').value.replace(/\/$/,'')!==c.signature.endpoint.replace(/\/$/,''))$('embedding-price').value='';$('embedding-collection').value=c.collection;$('embedding-dimensions').value=c.dimensions;$('embedding-model').value=c.signature.model_id;$('embedding-url').value=c.signature.endpoint;$('embedding-key').value='';$('embedding-status').textContent='已填入集合记录的模型签名，请填写密钥并测试。';}
async function loadDocuments(){const principals=await api('/auth/principals');for(const key of Object.keys(labels))delete labels[key];for(const p of principals.items)labels[p.user_id]=p.display_name;library=await api('/workbench/knowledge');documents=library.documents;for(const id of selectedDocuments)if(!documents.some(d=>d.document_id===id&&groupOfDocument(d)===documentGroup&&d.can_manage&&!d.withdrawn&&d.status==='succeeded'))selectedDocuments.delete(id);$('document-count').textContent=`/ ${documents.length}`;if(selected){selected=documents.find(d=>d.document_id===selected.document_id&&groupOfDocument(d)===documentGroup)||null;if(!selected){lifecycle.clear();previewConfig=null;$('document-detail').hidden=true;$('select-document').hidden=false;$('parsed-text').textContent='';$('chunk-preview').replaceChildren();}}renderDocuments();syncControls();}
function groupOfDocument(d){return d.withdrawn?'withdrawn':d.publication_state==='published'?'existing':'uploaded';}
function visibleDocuments(){const query=$('document-search').value.trim().toLowerCase();return documents.filter(d=>groupOfDocument(d)===documentGroup&&[d.filename,d.title,d.source].some(v=>v?.toLowerCase().includes(query)));}
function renderDocumentGroups(){
  const names={existing:'已有且可见的文档',uploaded:'新上传的文档',withdrawn:'已撤回的文档'};
  const help={existing:'已发布且当前身份可读的文档。',uploaded:'尚未发布的上传文档，包含解析、构建、待审批或退回状态；仅显示本人可管理或管理员可审阅的文档。通过审批并发布后，移入“已有且可见”。',withdrawn:'已撤回的文档不参与检索，可在历史版本中查看并处理。'};
  for(const group of Object.keys(names)){
    const count=documents.filter(d=>groupOfDocument(d)===group).length;
    $(group+'-document-count').textContent=count;
    const button=$('documents-'+group);button.setAttribute('aria-pressed',String(group===documentGroup));button.disabled=busy;
    button.hidden=group==='withdrawn'&&!count&&documentGroup!=='withdrawn';
  }
  $('document-group-help').textContent=help[documentGroup];
  $('document-list').setAttribute('aria-label',names[documentGroup]);
}
function changeDocumentGroup(group){
  if(busy||group===documentGroup)return;
  documentGroup=group;selectedDocuments.clear();selected=null;previewConfig=null;lifecycle.clear();
  $('document-detail').hidden=true;$('select-document').hidden=false;$('parsed-text').textContent='';$('chunk-preview').replaceChildren();$('permissions').replaceChildren();
  renderDocuments();syncControls();
}
function renderDocuments(){renderDocumentGroups();const query=$('document-search').value.trim().toLowerCase();const rows=visibleDocuments();const list=$('document-list');list.replaceChildren();if(!rows.length){list.append(node('div',query?'当前分组没有匹配文档。':{existing:'暂无已发布且可见的文档，可切换到“新上传”处理待发布资料。',uploaded:'暂无新上传的未发布文档。',withdrawn:'暂无已撤回的文档。'}[documentGroup],'empty-state'));syncBulkSelection();return;}for(const d of rows){const b=node('button','','document-item');b.type='button';b.disabled=busy;b.setAttribute('aria-pressed',String(selected?.document_id===d.document_id));b.append(node('strong',d.title||d.filename),node('small',`${d.filename} · ${d.version}`),node('small',d.source));let status=d.withdrawn?'已撤回':d.publication_state!=='published'&&d.status==='succeeded'?'未发布 · 构建后提交审批':d.status==='succeeded'?(d.indexed_chunks?`已索引 · ${d.indexed_chunks} 个切块`:d.chunks?'待索引':'待切分'):{queued:'等待解析',processing:'解析中',failed:'解析失败'}[d.status]||d.status;b.append(node('small',status+(d.owned?' · 我上传的':d.can_manage?' · 管理员可审阅':''),'doc-status'));b.addEventListener('click',()=>action(()=>selectDocument(d)));const row=node('div','','document-row'),choice=node('label','','document-select'),checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.dataset.documentId=d.document_id;checkbox.checked=selectedDocuments.has(d.document_id);checkbox.disabled=busy||!d.can_manage||!!d.withdrawn||d.status!=='succeeded';checkbox.setAttribute('aria-label',`选择 ${d.title||d.filename}`);checkbox.onchange=()=>{if(checkbox.checked)selectedDocuments.add(d.document_id);else selectedDocuments.delete(d.document_id);syncBulkSelection();};choice.append(checkbox);row.append(choice,b);list.append(row);}syncBulkSelection();}
async function selectDocument(d){if(documentGroup!==groupOfDocument(d)){documentGroup=groupOfDocument(d);selectedDocuments.clear();}selected=d;previewConfig=null;$('document-detail').hidden=false;$('select-document').hidden=true;$('document-title').textContent=d.title||d.filename;$('document-meta').textContent=`${d.source} · ${d.version} · ${d.document_id}`;$('preview-summary').textContent='';$('parsed-text').textContent='';$('chunk-preview').replaceChildren();$('permissions').replaceChildren();for(const [id,label] of Object.entries(labels)){const input=document.createElement('input');input.type='checkbox';input.value=id;input.checked=(d.user_ids||[]).includes(id);const lab=node('label',label);lab.prepend(input);$('permissions').append(lab);}renderDocuments();syncControls();await lifecycle.load(d);if(d.status==='succeeded'){if(!d.withdrawn&&d.can_read)await preview();else $('preview-summary').textContent=d.withdrawn?'已撤回：可在历史版本中检查原文。':'当前身份无正文阅读授权；可在访问权限中重新授权。';}else{ $('preview-summary').textContent=d.status==='failed'?`解析失败：${d.error_code}。可重试解析。`:'文档还在解析，请稍后刷新。';if(d.status==='failed'){const retry=node('button','重试解析','secondary-button');retry.type='button';retry.onclick=()=>action(async()=>{await api(`/ingestion/tasks/${encodeURIComponent(d.task_id)}/retry`,{});await loadDocuments();notice('已提交解析重试，请刷新查看状态。');});$('chunk-preview').append(retry);}}}
async function preview(){if(!selected||!$('chunk-form').reportValidity())return;const config=chunkConfig();if(config.overlap>=config.size)throw new Error('重叠字符数必须小于切块大小。');const r=await api(`/workbench/knowledge/${encodeURIComponent(selected.document_id)}/preview`,config);previewConfig=config;$('parsed-text').textContent=r.text;$('preview-summary').textContent=`预览 ${r.count} 个切块 · 原文 ${r.text.length} 字符${r.truncated?' · 仅展示前 100 块':''}`;$('chunk-preview').replaceChildren(...r.chunks.map((c,i)=>{const box=node('article','','chunk-card');box.append(node('small',`${String(i+1).padStart(2,'0')} / 字符 ${c.start_char}–${c.end_char} / ${(c.heading_paths||[]).join(' › ')}`),node('p',c.text));return box;}));syncControls();}
async function refresh(){await readiness();if(settingsPage)await loadSettings();else{await loadDocuments();await loadReviews();if(selected)await selectDocument(selected);}}
$('knowledge-page').hidden=settingsPage;$('settings-page').hidden=!settingsPage;document.querySelector(`.workspace-nav a[href="${location.pathname}"]`)?.setAttribute('aria-current','page');if(settingsPage){$('page-title').textContent='连接设置';$('page-subtitle').textContent='管理数据库和模型服务。';}
$('refresh').onclick=()=>action(refresh);$('identity').onchange=()=>{try{sessionStorage.setItem('knowledgeops-identity',$('identity').value);}catch{}selectedDocuments.clear();documentGroup='existing';queue=null;$('knowledge-progress').hidden=true;selected=null;lifecycle.clear();$('parsed-text').textContent='';$('chunk-preview').replaceChildren();$('permissions').replaceChildren();previewConfig=null;$('document-detail').hidden=true;$('select-document').hidden=false;action(refresh);};$('document-search').oninput=()=>{selectedDocuments.clear();renderDocuments();};for(const button of document.querySelectorAll('[data-document-group]'))button.onclick=()=>changeDocumentGroup(button.dataset.documentGroup);$('rerank-enabled').onchange=syncControls;$('existing-collection').onchange=chooseCollection;
$('test-database').onclick=()=>action(detectDatabase);
for(const name of ['embedding','rerank'])$("test-"+name).onclick=()=>action(async()=>{await api(`/workbench/connections/${name}/test`,name==='embedding'?embedding():provider(name));$(name+'-status').textContent='测试通过';});
$('connections-form').onsubmit=e=>{e.preventDefault();action(async()=>{await api('/workbench/connections',{database:database(),embedding:embedding(),rerank:$('rerank-enabled').checked?provider('rerank'):null},'PUT');for(const id of ['db-password','embedding-key','rerank-key'])$(id).value='';notice('连接已应用。返回工作台后请新建会话，再运行评测。');await readiness();await loadSettings();});};
function generation(){const g=provider('generation');const a=$('generation-input-price').value,b=$('generation-output-price').value;if(a!==''||b!==''){if(a===''||b==='')throw new Error('请同时填写输入与输出单价。');g.input_price=Number(a);g.output_price=Number(b);}return g;}
$('test-generation').onclick=()=>action(async()=>{await api('/workbench/model/test',generation());$('generation-status').textContent='连接与 JSON 输出测试通过';});$('generation-form').onsubmit=e=>{e.preventDefault();action(async()=>{await api('/workbench/model',generation(),'PUT');$('generation-key').value='';$('generation-status').textContent='模型已保存';await readiness();});};
$('chunk-form').onsubmit=e=>{e.preventDefault();action(preview);};$('chunk-form').oninput=()=>{previewConfig=null;syncControls();};$('build').onclick=()=>action(async()=>{if(!previewConfig)return;const r=await api('/index-jobs',{...previewConfig,document_id:selected.document_id,force:true});notice('索引任务已提交。新文档构建完成后，请刷新并在版本列表提交发布审批。');$('preview-summary').textContent='任务 '+r.job_id+' · 后台排队中';});

for(const view of ['original','chunks'])$('show-document-'+view).onclick=()=>{
  $('original-preview').hidden=view!=='original';$('chunk-preview').hidden=view!=='chunks';
  for(const name of ['original','chunks'])$('show-document-'+name).setAttribute('aria-pressed',String(name===view));
};
$('save-permissions').onclick=()=>action(async()=>{const ids=[...$('permissions').querySelectorAll('input:checked')].map(n=>n.value);await api(`/documents/${encodeURIComponent(selected.document_id)}/permissions`,{user_ids:ids},'PUT');notice('权限已保存。');await loadDocuments();await readiness();if(selected)await selectDocument(selected);});

function visibleOwnedDocuments(){return visibleDocuments().filter(d=>d.can_manage&&!d.withdrawn&&d.status==='succeeded');}
function syncBulkSelection(){
  for(const input of document.querySelectorAll('.document-select input')){const d=documents.find(d=>d.document_id===input.dataset.documentId);input.disabled=busy||!d?.can_manage||!!d.withdrawn||d.status!=='succeeded';}
  const rows=visibleOwnedDocuments(),count=rows.filter(d=>selectedDocuments.has(d.document_id)).length;
  $('selection-count').textContent=`已选 ${selectedDocuments.size} 份`;
  $('select-visible').checked=!!rows.length&&count===rows.length;
  $('select-visible').indeterminate=count>0&&count<rows.length;
  $('select-visible').disabled=busy||!rows.length;
  $('open-bulk-build').disabled=busy||!selectedDocuments.size;
  $('open-bulk-review').disabled=busy||!selectedDocuments.size;
}
$('select-visible').onchange=()=>{for(const d of visibleOwnedDocuments()){if($('select-visible').checked)selectedDocuments.add(d.document_id);else selectedDocuments.delete(d.document_id);}renderDocuments();};
function renderQueue(){
  if(!queue)return;
  const names={pending:'待处理',running:'处理中',succeeded:'成功',failed:'失败',skipped:'已跳过'};
  const counts=Object.fromEntries(Object.keys(names).map(k=>[k,queue.items.filter(x=>x.status===k).length]));
  $('knowledge-progress').hidden=false;
  $('queue-title').textContent=queue.kind==='approve'?'批量通过与发布':queue.kind==='review'?'批量提交审批':queue.kind==='upload'?'批量上传与解析':'批量构建索引';
  $('queue-summary').textContent=`${queue.running?(queue.stopRequested?'等待当前文档完成后停止':'处理中'):counts.pending?'已停止':'处理结束'} · 共 ${queue.items.length} 份 · 成功 ${counts.succeeded} · 跳过 ${counts.skipped} · 失败 ${counts.failed} · 未处理 ${counts.pending}`;
  $('queue-help').textContent=queue.running?'任务执行期间请保持页面打开。停止会等待当前文档完成；已完成的操作会保留。':'展开查看逐项结果；已完成的操作已保留。';
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
  $('queue-details').open=true;
  try{await queue.run(mode);}finally{
    $('queue-details').open=false;
    queueContext=null;busy=false;
    controls.forEach((control,i)=>control.disabled=disabled[i]);
    try{await loadDocuments();await loadReviews();await readiness();}catch(error){notice('列表刷新失败：'+error.message,true);}
    syncControls();renderQueue();
  }
}
function installQueue(kind,items,work,context=null){
  queue=new DocumentQueue(items,work,renderQueue);queue.kind=kind;
  queue.context=context||{identity:$('identity').value,fingerprint:library.runtime_fingerprint};
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
  const items=documents.filter(d=>selectedDocuments.has(d.document_id)&&d.can_manage&&!d.withdrawn&&d.status==='succeeded').map(d=>({id:d.document_id,name:d.title||d.filename,status:'pending'}));
  if(!items.length)return;
  $('bulk-build-dialog').close();
  action(async()=>{const result=await api('/index-jobs/batch',{...config,document_ids:items.map(x=>x.id)});notice(`已提交 ${result.items.length} 项后台索引任务${result.errors.length?'；'+result.errors.map(x=>x.message).join('；'):''}。可在任务中心查看，离开页面不影响已提交任务。`);});
};
$('open-upload').onclick=()=>{$('upload-status').textContent='';$('upload-limits').textContent=`单文件上限 ${Math.round((library?.max_document_bytes||1048576)/1024)} KB；每批最多 100 份、合计 50 MB。`;$('upload-dialog').showModal();};
$('close-upload').onclick=()=>$('upload-dialog').close();
$('upload-file').onchange=()=>{$('upload-source').disabled=$('upload-file').files.length>1;};
$('upload-form').onsubmit=event=>{
  event.preventDefault();if(busy||!$('upload-form').reportValidity())return;
  const files=[...$('upload-file').files],limit=library?.max_document_bytes||1048576;
  if(!files.length||files.length>100||files.reduce((n,f)=>n+f.size,0)>50*1024*1024){$('upload-status').textContent='请选择 1–100 个文件，每批合计不超过 50 MB。';return;}
  if(!library?.runtime_fingerprint){$('upload-status').textContent='请先刷新知识库状态。';return;}
  changeDocumentGroup('uploaded');
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

async function loadReviews(){
  if(settingsPage)return;
  const list=$('review-items'),expanded=new Set([...list.querySelectorAll('details[open]')].map(row=>row.dataset.reviewId));
  const r=await api('/document-reviews');list.replaceChildren();
  const pending=r.items.filter(x=>x.status==='pending');
  bulkApproval.setReviews(pending);
  $('review-count').textContent=`/ ${pending.length} 份待审批`;
  $('review-fold-summary').textContent=pending.length?`${pending.length} 份待审批`:'暂无待审批文档';
  if(!pending.length){list.append(node('p','当前没有待审批文档。','field-help'));return;}
  for(const review of pending){
    const row=node('details','','review-entry');row.dataset.reviewId=review.review_id;row.open=expanded.has(review.review_id);
    const summary=node('summary','');summary.append(node('strong',`${review.title||review.filename} · ${review.label}`),node('span','待审批','review-state'));row.append(summary);
    const body=node('div','','review-entry-body');
    body.append(node('p','发布后可读：'+review.user_ids.map(id=>labels[id]||id).join('、')));
    body.append(node('small',`提交者：${labels[review.submitted_by]||review.submitted_by}`));
    const actions=node('div','','actions');const open=node('button','查看待审原文','secondary-button');open.type='button';
    open.onclick=()=>action(async()=>{const doc=documents.find(d=>d.document_id===review.document_id);if(doc){await selectDocument(doc);await lifecycle.inspect(review.version_id);$('document-detail').scrollIntoView({behavior:'smooth'});}});actions.append(open);
    if(auth.mode==='demo'?$('identity').value==='u_admin_001':auth.canManage){
      const comment=document.createElement('textarea');comment.rows=2;comment.maxLength=1000;comment.placeholder='审批意见（退回时必填）';comment.setAttribute('aria-label','审批意见');body.append(comment);
      for(const [suffix,label] of [['approve','通过并发布'],['reject','退回修改']]){
        const b=node('button',label,suffix==='approve'?'primary-button':'secondary-button');b.type='button';
        b.onclick=()=>action(async()=>{if(suffix==='reject'&&!comment.value.trim())throw new Error('请填写退回原因。');await api(`/document-reviews/${encodeURIComponent(review.review_id)}/${suffix}`,{comment:comment.value.trim()});await refresh();notice(suffix==='approve'?'审批通过，版本和阅读权限已发布。':'已退回，原已发布版本保持有效。');});actions.append(b);
      }
    }
    body.append(actions);row.append(body);
    if(auth.canManage){const entry=node('div','','review-select-row');entry.append(bulkApproval.selectionControl(review),row);list.append(entry);}else list.append(row);
  }
}
$('refresh-reviews').onclick=()=>action(loadReviews);
