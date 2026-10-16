import {readEventStream} from './event-stream.js';
import {progressView,proposalView,receiptView,agentFailureMessage,currentProposal,proposalDecision} from './agent-view.js';
import * as cache from './experiment-cache.js';
const $ = id => document.getElementById(id);
try{const identity=sessionStorage.getItem('knowledgeops-identity');if([...$('identity').options].some(o=>o.value===identity))$('identity').value=identity;}catch{}
const welcomeTemplate=$('welcome')?.cloneNode(true);
function showWelcome(){if(welcomeTemplate)$('chat-log').replaceChildren(welcomeTemplate.cloneNode(true));}
const state = {view: "chat",datasetUpload:null,dataset:null, evaluationMode:false, importBusy:false, batches:[], activeBatch:null, config:null, identity:$('identity').value, busy:false, epoch:0, conversation:null, runs:[], current:null, a:null, b:null, last:null, agent:null, agentRequest:null, review:null, batchRunning:false, batchStop:false, failures:[], agentByIdentity:new Map()};
let cacheScope=null,cacheReady=false,cacheTimer=null,cacheQueue=Promise.resolve();
const savedFields=['semantic','keyword','rrf','rerank','semantic-k','keyword-k','fusion-k','rrf-constant','evidence-k','profile','search-mode','ef-search','temperature','top-p','output-limit','coverage-review','preset','dataset-split'];
const stages = {vector:'语义候选',keyword:'BM25 候选',fusion:'RRF 融合',rerank:'重排结果',selection_pool:'多查询选择池',final:'最终检索结果',evidence:'进入模型的证据'};
const statusNames = {answered:'已回答',refused:'证据不足 · 拒答',rejected:'输出未通过校验',running:'执行中',waiting_approval:'等待人工审批',completed:'已完成',cancelled:'已取消',failed:'执行失败',interrupted:'执行中断',needs_clarification:'需要补充信息'};
const timingNames = {routing:'意图判断',model_calls:'模型调用',output_validation:'输出结构校验',inspection_overhead:'快照与评测',history:'会话读取',retrieval:'检索总耗时',context_and_validation:'上下文与授权',generation:'生成与输出校验',commit:'保存会话',embedding:'查询向量化',vector_search:'向量查询',vector_authorization:'向量授权',keyword_corpus:'关键词语料准备',bm25:'BM25 排名',rrf:'RRF 融合',rerank:'重排',candidate_authorization:'候选授权',final_authorization:'最终授权',planning:'查询规划',evidence_selection:'覆盖选择',vector_total:'向量流程'};
function el(tag, className='', text='') { const node=document.createElement(tag); if(className)node.className=className; if(text!==undefined&&text!=='')node.textContent=String(text); return node; }
function button(text, action, cls='text-button') { const b=el('button',cls,text); b.type='button'; b.addEventListener('click',action); return b; }
function pct(value) { return typeof value==='number' ? `${(value*100).toFixed(1)}%` : '未评测'; }
function ms(value) { return typeof value==='number' ? value>=1000 ? `${(value/1000).toFixed(2)} s` : `${value.toFixed(1)} ms` : '—'; }
function line(label,value) { const n=el('div','metric-line'); n.append(el('span','',label),el('strong','',value)); return n; }
function group(title) { const n=el('section','metric-group'); n.append(el('h3','',title)); return n; }
function showNotice(message, error=false) { $('notice-text').textContent=message; $('notice').hidden=!message; $('notice').classList.toggle('error',error); $('notice').setAttribute('role',error?'alert':'status'); }
function resizeComposer(){const input=$('message');input.style.height='auto';input.style.height=Math.min(148,Math.max(34,input.scrollHeight||34))+'px';input.style.overflowY=input.scrollHeight>148?'auto':'hidden';}
async function api(path, options={}, identity=state.identity) {
  const controller=new AbortController(), timer=setTimeout(()=>controller.abort(),120000);
  try { const res=await fetch(path,{...options,signal:controller.signal,headers:{'X-Local-User-Id':identity,...(options.body?{'Content-Type':'application/json'}:{}),...options.headers}});
    const data=await res.json(); if(!res.ok){const error=new Error(`${data.error?.message||'请求失败'}（${data.error?.code||res.status}）`);error.code=data.error?.code;throw error;} return data;
  } catch(error) { if(error.name==='AbortError')throw new Error('请求等待超时。请检查服务状态后重试。'); throw error; } finally { clearTimeout(timer); }
}
function numeric(id) { const v=Number($(id).value); if(!Number.isFinite(v))throw new Error('请输入有效数值。'); return v; }
function controls() { return {top_k:numeric('evidence-k'),retrieval_profile:$('profile').value,
  generation:{model:$('model').value,output_limit:numeric('output-limit'),...(!samplingInactive()?{temperature:numeric('temperature'),top_p:$('top-p').value===''?null:numeric('top-p'),coverage_review:$('coverage-review').checked}:{})},
  retrieval:{semantic:$('semantic').checked,keyword:$('keyword').checked,rrf:$('rrf').checked,rerank:$('rerank').checked,semantic_k:numeric('semantic-k'),keyword_k:numeric('keyword-k'),fusion_k:numeric('fusion-k'),rrf_constant:numeric('rrf-constant'),mode:$('search-mode').value,ef_search:numeric('ef-search')}}; }
function samplingInactive() { return !state.config?.generation.ready; }
function validateControls() {
  if(!state.config)return '服务配置尚未加载。';
  if(!$('parameters-form').checkValidity())return '参数超出允许范围，请检查数值。';
  const c=controls(),r=c.retrieval;
  if(!r.semantic&&!r.keyword)return '至少开启一种检索方式。';
  if(r.semantic&&r.keyword&&!r.rrf)return '两路检索同时开启时，需要开启 RRF 融合。';
  const available=r.semantic&&r.keyword?r.fusion_k:r.semantic?r.semantic_k:r.keyword_k;
  if(c.top_k>available)return '最终证据上限不能超过上游候选数。';
  if(c.generation.output_limit>=state.config.generation.input_limit)return '输出预算必须小于输入预算。';
  return '';
}
function updateControls() {
  const disabled=state.busy||state.importBusy||!state.config;
  for(const id of ['retrieval-fields','evidence-fields','generation-fields'])$(id).disabled=disabled;
  $('reset-config').disabled=disabled;
  $('semantic-k').disabled=disabled||!$('semantic').checked; $('keyword-k').disabled=disabled||!$('keyword').checked;
  const dual=$('semantic').checked&&$('keyword').checked;
  for(const id of ['rrf','fusion-k','rrf-constant'])$(id).disabled=disabled||!dual;
  $('rerank').disabled=disabled||!state.config?.retrieval.rerank_available;
  $('search-mode').disabled=disabled||!$('semantic').checked;
  $('ef-search').disabled=disabled||!$('semantic').checked||$('search-mode').value!=='hnsw';
  for(const id of ['temperature','top-p','coverage-review','temperature-range','top-p-range'])$(id).disabled=disabled||samplingInactive();
  $('sampling-note').textContent=samplingInactive()?'请先连接真实模型。':'参数会发送给供应商；供应商不回显实际生效值。';
  const err=!state.busy?validateControls():'';
  $('config-error').textContent=err; $('config-error').hidden=!err;
  $('toggle-parameters').textContent=err?'运行参数 · 需检查':'运行参数';
  $('send').disabled=state.evaluationMode||state.importBusy||state.busy||!state.config?.generation.ready||!!err||['running','waiting_approval','interrupted'].includes(state.agent?.status);
  for(const b of document.querySelectorAll('[data-agent]'))b.disabled=state.busy||['running','waiting_approval','interrupted'].includes(state.agent?.status);
  $('identity').disabled=state.busy; $('question').disabled=state.busy; $('new-conversation').disabled=state.busy; $('model-settings').disabled=state.busy; $('intent').disabled=state.busy;
  $('send').textContent=state.busy?'正在处理…':'发送 ↑';
  const waiting=state.agent?.status==='waiting_approval';
  const expired=waiting&&state.agent.pending?.expires_at*1000<=Date.now();
  $('composer-note').textContent=expired?'请关闭过期预览后重新发起任务':waiting?'先确认或暂不提交工单，再继续会话':'Enter 发送 · Shift + Enter 换行';
  $('message').placeholder=waiting?'请先处理上方工单，再继续会话…':'描述问题，或交代一个任务…';
  $('pipeline-badge').textContent=[$('semantic').checked?'语义':'',$('keyword').checked?'BM25':''].filter(Boolean).join(' ＋ ')+(dual?' → RRF':'')+($('rerank').checked?' → 重排':'')+' → 证据';
  $('parameter-scope').textContent='问答检索与生成参数 · 任务沿用执行预算';
  $('rerun').disabled=state.busy||!state.last||!!err; $('compare').disabled=!state.runs.length||state.busy; $('export').disabled=(!state.runs.length&&!state.failures.length)||state.busy;
  $('batch').disabled=!state.config||state.busy&&!state.batchRunning||!!err; $('batch').textContent=state.batchRunning?'停止后续题目':'运行当前测试集';
  const model=state.config?.generation;
  $('model-badge').textContent=model?.model_id||'尚未连接模型';
  $('new-conversation').disabled=state.busy||state.importBusy||['running','waiting_approval','interrupted'].includes(state.agent?.status);
  $('batch').disabled ||= state.importBusy||!state.dataset||!state.evaluationMode||!state.config?.generation.ready||['running','waiting_approval','interrupted'].includes(state.agent?.status);
  $('rerun').disabled ||= !state.config?.generation.ready||['running','waiting_approval','interrupted'].includes(state.agent?.status);
  $('start-dataset').disabled=$('batch').disabled;
  $('start-dataset').textContent=state.batchRunning?'停止后续题目':'开始评测';
  for(const id of ['preset','dataset-split','replace-dataset','welcome-import'])if($(id))$(id).disabled=state.busy||state.importBusy;
  $('question').disabled=state.busy||state.importBusy;
  $('identity').disabled=state.busy||state.importBusy;
  $('model-settings').disabled=state.busy||state.importBusy;
  $('composer').hidden=state.evaluationMode;
  $('dataset-panel').hidden=!state.evaluationMode||!state.dataset;
  const latest=state.batches.at(-1);$('resume-batch').hidden=!latest||latest.status==='completed'||!state.datasetUpload||latest.dataset.fingerprint!==state.dataset?.fingerprint;$('resume-batch').disabled=state.busy||state.importBusy;$('clear-cache').disabled=state.busy||state.importBusy||!cacheScope;
  renderPipeline();renderWorkspaceView();resizeComposer();
}
function applyDefaults() {
  const c=state.config; if(!c)return; const strategy=c.retrieval.strategy;
  $('preset').value='custom';$('preset-status').textContent='服务端默认';
  $('semantic').checked=!strategy.startsWith('bm25'); $('keyword').checked=!strategy.startsWith('vector'); $('rrf').checked=true; $('rerank').checked=strategy.endsWith('_rerank')&&c.retrieval.rerank_available;
  for(const id of ['semantic-k','keyword-k','fusion-k'])$(id).value=c.retrieval.candidate_k;
  $('rrf-constant').value=c.retrieval.rrf_constant; $('evidence-k').value=Math.min(8,c.retrieval.candidate_k);
  $('temperature').value=c.generation.temperature; $('top-p').value=c.generation.top_p??''; $('output-limit').value=c.generation.output_limit; $('coverage-review').checked=c.generation.coverage_review;
  $('profile').value='ranked'; $('search-mode').value='exact'; $('ef-search').value=40;
  $('model').replaceChildren(...c.generation.models.map(m=>{const o=el('option','',m.label);o.value=m.id;return o;})); $('rerank-label').textContent=c.retrieval.rerank_available?c.retrieval.rerank_provider:'服务端未配置'; updateControls();
}
function renderQuestions() {
  const free=el('option','','不选择测试集 · 查看运行记录');free.value='';
  const load=el('option','','导入测试集（JSONL）…');load.value='import';
  $('question').replaceChildren(free,load);
  if(state.dataset){const selected=el('option','',`测试集 · ${state.dataset.name}`);selected.value='dataset';$('question').append(selected);}
  $('question').value=state.evaluationMode?'dataset':'';
}
async function loadConfig(reset=true) {
  const epoch=state.epoch;
  try { const config=await api('/workbench/config'); if(epoch!==state.epoch)return;
    state.config=config; $('connection').textContent='本地服务已连接'; $('connection').classList.add('ready'); renderQuestions(); if(reset)applyDefaults();updateControls(); showNotice(config.generation.ready?'':'点击右上角“模型设置”，连接真实模型后开始会话。');
    if(!state.agent){const active=await api('/workbench/tasks');if(epoch!==state.epoch)return;if(active.tasks.length){state.agent=active.tasks[0];state.conversation=state.agent.conversation_id;state.agentByIdentity.set(state.identity,state.agent);$('welcome')?.remove();renderAgent(state.agent);if(state.agent.status==='running')pollAgent(state.agent.run_id,state.identity,epoch);}}
  } catch(error) { $('connection').textContent='服务连接失败'; $('connection').classList.remove('ready'); showNotice(error.message+' 请初始化知识库并检查服务。',true);updateControls(); }
}
function selectedId() { return null; }
function messageLabel(role){
  const label=el('div','message-label'),avatar=el('span','message-avatar',role==='user'?'你':role==='error'?'!':'K');
  avatar.setAttribute('aria-hidden','true');
  label.append(avatar,el('strong','',role==='user'?'你':role==='error'?'请求失败':'KnowledgeOps'));
  return label;
}
function message(role,text,run=null) {
  const n=el('article',`message ${role}`),label=messageLabel(role); n.append(label); const body=el('div','message-body');
  if(run?.response.claims?.length){const refs=run.response.citations;for(const claim of run.response.claims){const p=el('p','claim',claim.text);for(const cid of claim.citations){const i=refs.findIndex(c=>c.chunk_id===cid);if(i>=0)p.append(button(`[${i+1}]`,()=>openSource(refs[i]),'citation-button'));}body.append(p);}} else body.textContent=text;
  if(role==='assistant'&&run?.response.citations?.length){
    const layout=el('div','answer-layout'),sources=el('aside','answer-sources');
    sources.setAttribute('aria-label','回答参考来源');sources.append(el('h4','','参考来源'));
    const seen=new Set();
    for(const ref of run.response.citations){
      const key=(ref.document_id||ref.source||ref.title||ref.chunk_id)+'/'+(ref.version||'');
      if(seen.has(key))continue;seen.add(key);
      const item=el('div','answer-source'),title=ref.title||ref.source||'知识库依据';
      const link=button(title,()=>openSource(ref),'source-link');link.setAttribute('aria-label','查看来源：'+title);item.append(link);
      if(ref.version)item.append(el('small','',ref.version));sources.append(item);
    }
    layout.append(body,sources);n.append(layout);
  }else n.append(body);
  if(run){const foot=el('div','message-footer');foot.append(el('span','',statusNames[run.response.status]||run.response.status),el('span','',`${run.response.context.evidence_count} 个证据切块 · ${ms(run.response.context.timings_ms.total)}`),button('查看这次运行',()=>{selectRun(run.id);showInspector();}),button('人工评审',()=>openReview(run.id)));n.append(foot);}
  $('chat-log').append(n); $('chat-log').scrollTop=$('chat-log').scrollHeight;return n;
}
async function runChat(messageText, {fresh=false,batch=false,questionId=null,unified=false,dataset=null,frozenControls=null,runtime=null,batchId=null}={}) {
  const error=validateControls(); if(error)throw new Error(error);
  const epoch=state.epoch,identity=state.identity,c=frozenControls||controls(), qid=questionId??selectedId(messageText), conversation=fresh||qid?null:state.conversation;
  const request={message:messageText,conversation_id:conversation,...c,...(qid?{question_id:qid}:{}),...(dataset?{dataset_id:dataset.id,runtime_fingerprint:runtime}:{})};
  if(unified){if(!state.agentRequest||state.agentRequest.message!==messageText||state.agentRequest.conversation!==conversation)state.agentRequest={message:messageText,conversation,key:crypto.randomUUID()};Object.assign(request,{intent:$('intent').value,request_key:state.agentRequest.key});}
  const evaluationRun=batch||state.view==='evaluation';
  const pending=el('div','pending','正在处理…');
  if(!evaluationRun){$('welcome')?.remove();message('user',messageText);$('chat-log').append(pending);}
  state.busy=true;updateControls();
  try {let response=await (!evaluationRun?
      streamRequest(unified?'/workbench/message/stream':'/workbench/chat/stream',request,identity,pending,epoch):
      batchRequest(unified?'/workbench/message':'/workbench/chat',{method:'POST',body:JSON.stringify(request)},identity,batch,pending));
    if(epoch!==state.epoch)return null;
    pending.remove();
    if(unified){state.agentRequest=null;if(response.kind==='agent'){state.conversation=response.conversation_id;state.agent=response.agent;state.agentByIdentity.set(identity,state.agent);$('message').value='';renderAgent(state.agent);pollAgent(state.agent.run_id,identity,epoch);return null;}response=response.response;}
    if(!batch&&!fresh)state.conversation=qid?null:response.conversation_id;
    const run={view:evaluationRun?"evaluation":"chat",batch_id:batchId,dataset:dataset?{id:dataset.id,name:dataset.name,fingerprint:dataset.fingerprint}:null,id:response.experiment_id,number:state.runs.length+1,message:messageText,request,response,created:new Date().toISOString(),review:null};
    state.runs.push(run);state.last={message:messageText,questionId:qid,dataset};state.current=run.id; if(!evaluationRun)message('assistant',response.answer,run);renderMetrics(run);renderEvidence(run);renderExperiments(); if(!evaluationRun)$('message').value='';return run;
  } catch(error) { pending.remove();if(epoch===state.epoch){state.failures.push({batch_id:batchId,request,identity,error:error.message,created:new Date().toISOString()});if(!evaluationRun)message('error',error.message);showNotice(error.message,true);renderExperiments();}throw error;
  } finally { if(epoch===state.epoch){state.busy=batch;updateControls();} }
}
async function streamRequest(path,body,identity,pending,epoch){
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),180000);
  pending.className='message assistant streaming-message';
  const status=el('p','streaming-status','正在准备回答…'),preview=el('div','message-body streaming-preview');
  pending.replaceChildren(messageLabel('assistant'),status,preview);
  const phases={accepted:'已收到问题…',routing:'正在判断问题与任务类型…',retrieval:'正在检索授权知识库…',
    generating:'正在生成 · 内容待校验',review:'正在复查内容覆盖 · 内容待校验',
    validating:'正在校验回答与引用…',saving:'正在保存最终结果…'};
  const scroll=()=>{const log=$('chat-log');if(log.scrollHeight-log.scrollTop-log.clientHeight<200)log.scrollTop=log.scrollHeight;};
  try{
    const response=await fetch(path,{method:'POST',signal:controller.signal,headers:{'X-Local-User-Id':identity,'Content-Type':'application/json'},body:JSON.stringify(body)});
    if(!response.ok){const data=await response.json();const error=new Error(data.error?.message||'请求失败');error.code=data.error?.code;throw error;}
    return await readEventStream(response,(kind,data)=>{
      if(epoch!==state.epoch)return;
      if(kind==='phase')status.textContent=phases[data.stage]||'正在处理…';
      if(kind==='reset')preview.textContent='';
      if(kind==='preview'){preview.textContent=data.text;scroll();}
    });
  }catch(error){
    if(error.name==='AbortError')throw new Error('流式响应超时，回答未完成，请重试。');
    throw error;
  }finally{clearTimeout(timer);}
}
async function batchRequest(path,options,identity,batch,pending) {
  for(let attempt=0;attempt<7;attempt++){
    try{return await api(path,options,identity);}catch(error){
      if(!batch||error.code!=='RATE_LIMIT'||attempt===6)throw error;
      pending.textContent='达到服务端请求速率限制，等待恢复后继续本题…';
      await new Promise(resolve=>setTimeout(resolve,10000));
      if(state.batchStop)throw new Error('批量运行已停止，当前待重试题未执行。');
    }
  }
}
function selectRun(id) { const run=state.runs.find(r=>r.id===id);if(!run)return;state.current=id;state.last={message:run.message,questionId:run.request.question_id,dataset:run.dataset};renderMetrics(run);renderEvidence(run);$('inspect-title').textContent='证据与指标';renderExperiments();scheduleSave(); }
function crossMetric(run,stage='evidence') {const e=run.response.evaluation;return e.question_type==='cross_document'&&e.status==='measured'?e.stages[stage]?.all_required_sources_recalled:null;}
function crossText(value) {return value===true?'完整 · 1/1':value===false?'未完整 · 0/1':'不适用';}
function timingRow(name,value,total,sub=false) {const row=el('div',`timing-row${sub?' sub':''}`),track=el('div','track'),bar=el('div','bar');bar.style.width=`${Math.max(1,Math.min(100,value/Math.max(total,1)*100))}%`;track.append(bar);row.append(el('span','',timingNames[name]||name),track,el('span','value',ms(value)));return row;}
function usageSummary(run) {const calls=run.response.context.model_usage||[];const full=calls.length>0&&calls.every(c=>Number.isInteger(c.usage?.prompt_tokens)&&Number.isInteger(c.usage?.completion_tokens));return full?{input:calls.reduce((s,c)=>s+c.usage.prompt_tokens,0),output:calls.reduce((s,c)=>s+c.usage.completion_tokens,0)}:null;}
function renderMetrics(run) {
  const answer=$('inspection-answer');answer.hidden=false;answer.replaceChildren(el('h3','',run.message),el('p','',run.response.answer));
  const box=$('metrics-panel');box.replaceChildren();$('run-number').textContent=`RUN ${String(run.number).padStart(2,'0')}`;
  const r=run.response,c=r.context,t=c.timings_ms,e=r.evaluation,m=e.stages?.evidence;
  const hero=el('div','metric-hero'),main=el('div'),val=el('strong','',t.total>=1000?(t.total/1000).toFixed(2):t.total.toFixed(1));main.append(el('small','','总耗时'),val,el('span','unit',t.total>=1000?'s':'ms'));
  const count=el('div','mini-stat');count.append(el('small','','实际引用'),el('strong','',r.citations.length),el('span','unit','条'));hero.append(main,count);box.append(hero);
  const coverage=group('证据与来源'),evidence=r.retrieval_trace.stages.evidence||[];
  coverage.append(line('检索结果 → 入模证据',`${c.retrieval_hit_count} → ${c.evidence_count} 切块`),line('入模独立文档',`${new Set(evidence.map(h=>h.document_id)).size} 份`),line('预算裁掉的证据',`${c.omitted_evidence_ids.length} 切块`),line('来源召回率',pct(m?.source_recall_at_k)),line('章节相关精确率',pct(m?.precision_at_k)),line('必要章节字符覆盖',pct(m?.section_char_coverage_at_k)),line('跨文档完整召回',crossText(crossMetric(run))));
  const cap=e.status==='measured'?`${e.question_id} · ${e.dataset||'标注题'}，必要来源 ${m?.expected_source_count||0} 份。${e.precision_scope||'精确率以章节坐标交集判定'}。`:e.reason||'未评测';coverage.append(el('p','metric-caption',cap));box.append(coverage);
  const timings=group('阶段耗时');for(const name of ['routing','history','retrieval','context_and_validation','generation','commit','inspection_overhead'])if(typeof t[name]==='number')timings.append(timingRow(name,t[name],t.total));
  const detail=el('details');detail.append(el('summary','','展开检索子阶段'));for(const [name,value]of Object.entries(r.retrieval_trace.timings_ms))if(typeof value==='number')detail.append(timingRow(name,value,t.retrieval,true));
  const gd=el('details');gd.append(el('summary','','展开生成子阶段'));for(const name of ['model_calls','output_validation'])if(typeof t[name]==='number')gd.append(timingRow(name,t[name],t.generation,true));timings.append(gd);
  timings.append(detail,el('p','metric-caption','子阶段包含在检索总耗时内；总耗时另含授权、编排与保存开销。此处不代表模型首 token 延迟。'));box.append(timings);
  const tokens=group('生成用量'),u=usageSummary(run);tokens.append(line('输入 / 输出 tokens',u?`${u.input} / ${u.output}`:'供应商未提供'),line('上下文输入估算',`${c.input_token_estimate} tokens`),line('回答生成调用',`${c.generation_attempts} 次`),line('查询规划调用',`${r.retrieval_trace.planning.model_calls||0} 次`),line('重排状态',r.retrieval_trace.rerank.status||'disabled'),line('覆盖复查',c.coverage_review),line('生成费用','未配置计价'),line('重排费用',r.retrieval_trace.rerank.estimated_cost===0?'0':r.retrieval_trace.rerank.estimated_cost==null?'未知':String(r.retrieval_trace.rerank.estimated_cost)));
  tokens.append(el('p','metric-caption','上下文估算采用 UTF-8 字节与框架开销，不能替代供应商实际 token 统计。'));box.append(tokens);
  const review=group('答案与引用评审');review.append(line('答案正确性',run.review?.correctness==='pass'?'人工通过':run.review?.correctness==='fail'?'人工未通过':'待人工评审'),line('引用支持结论',run.review?.citation_support==='pass'?'人工通过':run.review?.citation_support==='fail'?'人工未通过':'待人工评审'),el('p','metric-caption','来源 ID、版本和权限校验通过，不等于结论语义正确。'),button('标注本次输出',()=>openReview(run.id),'secondary-button'));box.append(review);
  const config=el('details');config.append(el('summary','','本次实际参数'),el('pre','config-preview',JSON.stringify(r.effective_config,null,2)));box.append(config);
}
function renderEvidence(run) {
  const box=$('evidence-list');box.replaceChildren();const stage=$('evidence-stage').value,hits=run?.response.retrieval_trace.stages[stage]||[];box.classList.toggle('empty-state',!hits.length);
  if(!hits.length){box.textContent=run?'此阶段未启用或没有当前可访问的结果。':'还没有检索结果。';return;}
  if(run.response.effective_config.retrieval_profile==='multi_query'&&!['final','evidence','selection_pool'].includes(stage))box.append(el('p','field-help','多查询阶段按查询顺序拼接并去重，序号不代表全局排名。'));
  const metric=run.response.evaluation.stages?.[stage];if(metric)box.append(el('p','field-help',`来源召回 ${pct(metric.source_recall_at_k)} · 精确率 ${pct(metric.precision_at_k)} · 去重 ${metric.retrieved_count} 切块`));
  const ids=new Set();let rank=0;
  for(const hit of hits){if(ids.has(hit.chunk_id))continue;ids.add(hit.chunk_id);rank++;const card=el('article','evidence-card'),heading=el('h3');heading.append(el('span','rank',rank),el('span','',hit.citation.title||hit.source));card.append(heading,el('p','',`${hit.citation.heading_paths.map(p=>p.join(' › ')).join(' / ')} · v${hit.version}`),el('p','excerpt',hit.text));
    if(hit.ranks)card.append(el('p','',Object.entries(hit.ranks).map(([name,n])=>`${name} #${n}`).join(' · ')));
    const actions=el('div','actions');actions.append(el('span','evidence-score',`score ${Number(hit.score).toFixed(4)}`),button('打开原文 ↗',()=>openSource(hit.citation)));card.append(actions);box.append(card);
  }
}
async function openSource(citation) {
  const epoch=state.epoch,dialog=$('source-dialog');$('source-title').textContent=citation.title||'引用原文';$('source-meta').textContent=`${citation.source} · 版本 ${citation.version} · 行 ${citation.location.start_line}–${citation.location.end_line}`;$('source-text').textContent='正在重新检查权限并读取原文…';dialog.showModal();
  try {const data=await api(`/citations/${encodeURIComponent(citation.chunk_id)}?chunk_set_id=${encodeURIComponent(citation.chunk_set_id)}`);if(epoch===state.epoch)$('source-text').textContent=data.text;}catch(error){if(epoch===state.epoch)$('source-text').textContent=error.message+' 原文可能已更新或权限已撤销。';}
}
function renderExperiments() {
  scheduleSave();
  renderBatchSummary();
  $('experiment-count').textContent=state.runs.length;const body=$('experiment-rows');body.replaceChildren();
  if(!state.runs.length){const tr=el('tr'),td=el('td','table-empty','每次运行都保存实际参数与结果，便于复盘。');td.colSpan=7;tr.append(td);body.append(tr);$('experiment-scope').textContent=`当前页尚无成功响应，失败请求 ${state.failures.length} 次。失败记录可随 JSON 导出。`;updateControls();return;}
  for(const run of [...state.runs].reverse()){const r=run.response,m=r.evaluation.stages?.evidence,c=r.effective_config, tr=el('tr'),title=el('td'),open=button(`${String(run.number).padStart(2,'0')} / ${run.request.question_id||'自由问题'}`,()=>{selectRun(run.id);showInspector();},'row-open');tr.classList.toggle('selected-run',run.id===state.current);title.append(open,el('span','row-title',run.message),el('span','row-subtitle',`${c.generation.model_id} · ${statusNames[r.status]}`));tr.append(title);
    const strategy=el('td');strategy.append(el('span','',`${c.retrieval.strategy} · ${c.retrieval_profile}`),el('span','row-subtitle',`V${c.retrieval.semantic_k} / B${c.retrieval.keyword_k} / F${c.retrieval.fusion_k} / E${c.top_k}`));tr.append(strategy);
    const hits=r.retrieval_trace.stages.evidence||[];for(const value of [`${hits.length} / ${new Set(hits.map(h=>h.document_id)).size}`,pct(m?.source_recall_at_k),crossText(crossMetric(run)),ms(r.context.timings_ms.total)])tr.append(el('td','',value));
    const reviewCell=el('td','run-review-cell');
    reviewCell.append(el('span','row-subtitle',run.review?.correctness==='pass'?'人工通过':run.review?.correctness==='fail'?'人工未通过':'待评审'),button(run.review?'修改评审':'评审',()=>openReview(run.id),'text-button'));
    tr.append(reviewCell);
    body.append(tr);
  }
  const measured=state.runs.filter(r=>r.response.evaluation.status==='measured'),cross=measured.filter(r=>r.response.evaluation.question_type==='cross_document'),labels=state.runs.filter(r=>r.review?.correctness),passes=labels.filter(r=>r.review.correctness==='pass');
  const durations=state.runs.map(r=>r.response.context.timings_ms.total).sort((a,b)=>a-b), quantile=p=>durations[Math.max(0,Math.ceil(p*durations.length)-1)];
  $('experiment-scope').textContent=`当前页 ${state.runs.length} 次运行（含重跑，非独立题数） · 请求失败 ${state.failures.length} 次 · 标注题 ${measured.length} 次 · 跨文档完整 ${cross.filter(r=>crossMetric(r)).length}/${cross.length} · 人工答案正确 ${passes.length}/${labels.length}${labels.length?`（${pct(passes.length/labels.length)}）`:'（未评审）'} · 成功请求 P50 ${ms(quantile(.5))} / P95 ${ms(quantile(.95))}。记录自动保存在本机浏览器。`;updateControls();
}
function renderBatchSummary(){
  const box=$('batch-results');box.replaceChildren();const batch=state.batches.at(-1);if(!batch)return;
  const runs=state.runs.filter(r=>r.batch_id===batch.id), measured=runs.filter(r=>typeof r.response.evaluation.stages?.evidence?.source_recall_at_k==='number'),cross=runs.filter(r=>crossMetric(r)!==null),labels=runs.filter(r=>r.review?.correctness);
  const durations=runs.map(r=>r.response.context.timings_ms.total).sort((a,b)=>a-b),quantile=p=>durations.length?durations[Math.ceil(p*durations.length)-1]:null;
  box.append(el('h3','',`最近批次 · ${batch.dataset.name} / ${batch.split}`),el('p','field-help',`${batch.completed} 次响应 · ${batch.errors} 次失败 · 计划 ${batch.planned} 题；按当前身份与固定参数运行。`));
  const stats=el('div','batch-stats');stats.append(line('平均来源召回',measured.length?`${pct(measured.reduce((n,r)=>n+r.response.evaluation.stages.evidence.source_recall_at_k,0)/measured.length)} · ${measured.length} 题`:'未评测'),line('跨文档来源完整',cross.length?`${cross.filter(r=>crossMetric(r)).length} / ${cross.length}`:'未评测'),line('人工答案正确',labels.length?`${labels.filter(r=>r.review.correctness==='pass').length} / ${labels.length}`:'未评审'),line('响应耗时 P50 / P95',`${ms(quantile(.5))} / ${ms(quantile(.95))}`));box.append(stats);
  if(runs.length){
    const pending=runs.filter(r=>!reviewComplete(r)),actions=el('div','batch-review-actions');
    actions.append(el('span','field-help','完整评审 '+(runs.length-pending.length)+' / '+runs.length+' 条（答案与引用两项）'),
      button(pending.length?'开始逐条评审':'查看本轮评审',()=>openReview((pending[0]||runs[0]).id),'secondary-button'));
    box.append(actions);
  }
}
function comparisonCandidates(runs,baselineId,sameQuestion){
  const baseline=runs.find(r=>r.id===baselineId);
  return runs.filter(r=>r.id!==baselineId&&(!sameQuestion||r.message===baseline?.message));
}
function comparisonLabel(run){
  const c=run.response.effective_config,g=c.generation,r=c.retrieval;
  const strategy={vector:'语义检索',bm25:'关键词检索',hybrid:'语义＋关键词',hybrid_rerank:'混合检索＋重排',vector_rerank:'语义检索＋重排'}[r.strategy]||r.strategy;
  return '运行 '+run.number+' · '+(run.request.question_id||run.message.slice(0,24))+' · '+g.model_id+' · '+strategy+' · 证据 '+c.top_k+' 块';
}
function compareRuns(){
  if(!state.runs.length)return;
  const runs=[...state.runs].reverse();
  const prior=state.runs.find(r=>r.id===state.a),pair=runs.find(r=>runs.some(other=>other.id!==r.id&&other.message===r.message));
  state.a=prior?.id||pair?.id||state.current||runs[0].id;
  $('comparison-baseline').replaceChildren(...runs.map(r=>new Option(comparisonLabel(r),r.id)));
  $('comparison-baseline').value=state.a;
  $('comparison-same-question').checked=true;updateComparisonCandidates();
  if(!$('compare-dialog').open)$('compare-dialog').showModal();
}
function updateComparisonCandidates(){
  state.a=$('comparison-baseline').value;
  const choices=comparisonCandidates([...state.runs].reverse(),state.a,$('comparison-same-question').checked);
  const previous=state.b;state.b=choices.some(r=>r.id===previous)?previous:choices[0]?.id||null;
  $('comparison-candidate').replaceChildren(...(choices.length?choices.map(r=>new Option(comparisonLabel(r),r.id)):[new Option('暂无可对照的其他运行','')]));
  $('comparison-candidate').value=state.b||'';$('comparison-candidate').disabled=!choices.length;
  renderComparison();scheduleSave();
}
function renderComparison(){
  const a=state.runs.find(r=>r.id===state.a),b=state.runs.find(r=>r.id===state.b),box=$('comparison-content');box.replaceChildren();
  if(!a||!b||a.id===b.id){
    $('comparison-selection-note').textContent='目前没有同一道题的另一条记录。先在列表中打开题目，调整运行参数后点击“用当前参数重跑”，再回来对比。';
    box.append(el('p','comparison-empty','一次批量评测通常每题只有一条记录；需要将同一道题再运行一次，才能比较参数效果。'));return;
  }
  $('comparison-selection-note').textContent='基准：运行 '+a.number+' → 对照：运行 '+b.number+' · '+(a.message===b.message?'同一道题':'不同题目');
  const same=a.message===b.message&&JSON.stringify(a.response.identity)===JSON.stringify(b.response.identity)&&!a.request.conversation_id&&!b.request.conversation_id&&a.response.evaluation.dataset_fingerprint===b.response.evaluation.dataset_fingerprint&&a.response.corpus_snapshot?.fingerprint===b.response.corpus_snapshot?.fingerprint&&!a.response.corpus_changed_during_run&&!b.response.corpus_changed_during_run;
  box.append(el('p','comparison-question',a.message===b.message?'对比问题：'+a.message:'基准问题：'+a.message+'\n对照问题：'+b.message));
  box.append(el('p','notice'+(same?'':' error'),same?'对比条件一致：题目、身份、会话历史与知识库版本已核对。':'对比条件不一致。可查看两次记录，但不能将效果差异归因于参数变化。'));
  const names={model_id:'模型',strategy:'检索方式',retrieval_profile:'检索方案',top_k:'证据数量',semantic_k:'语义候选数',keyword_k:'关键词候选数',fusion_k:'融合保留数',temperature:'温度',top_p:'Top P',output_limit:'输出上限',rrf_constant:'RRF 常数',mode:'搜索模式',ef_search:'搜索宽度',coverage_review:'覆盖复查',semantic:'语义检索',keyword:'关键词检索',rerank:'重排'};
  const changes=group('哪些配置变了'),walk=(x,y,path='')=>{for(const key of new Set([...Object.keys(x||{}),...Object.keys(y||{})])){
    const name=path?path+'.'+key:key,v=x?.[key],w=y?.[key];
    if(v&&w&&typeof v==='object'&&typeof w==='object'&&!Array.isArray(v))walk(v,w,name);
    else if(JSON.stringify(v)!==JSON.stringify(w))changes.append(line(names[key]||name,(JSON.stringify(v)??'—')+' → '+(JSON.stringify(w)??'—')));
  }};
  walk(a.response.effective_config,b.response.effective_config);
  if(changes.childElementCount===1)changes.append(el('p','field-help','配置相同，可观察两次生成结果与耗时的波动。'));box.append(changes);
  const grid=el('div','comparison-grid');
  for(const [label,run]of [['基准运行',a],['对照运行',b]]){
    const col=el('section','compare-column'),r=run.response,m=r.evaluation.stages?.evidence;
    col.append(el('h3','',label+' · 运行 '+run.number),el('p','field-help',comparisonLabel(run)),
      line('来源召回 · 实际入模',pct(m?.source_recall_at_k)),line('章节字符覆盖',pct(m?.section_char_coverage_at_k)),
      line('跨文档完整',crossText(crossMetric(run))),line('总耗时',ms(r.context.timings_ms.total)),
      line('人工评审',run.review?.correctness==='pass'?'通过':run.review?.correctness==='fail'?'未通过':'待评审'),
      el('h4','','实际回答'),el('p','answer-preview',r.answer),
      button('评审这次回答',()=>{$('compare-dialog').close();openReview(run.id);},'secondary-button'));grid.append(col);
  }
  box.append(grid);
}
function reviewComplete(run){return !!(run.review?.correctness&&run.review?.citation_support);}
function reviewQueue(run){return state.runs.filter(r=>run.batch_id?r.batch_id===run.batch_id:!r.batch_id);}
function reviewDraft(){
  return {correctness:$('correctness').value,citation_support:$('citation-support').value,reviewer:$('reviewer').value,notes:$('review-notes').value};
}
let reviewSaving=false;
const reviewDrafts=new Map();
function stashReviewDraft(){if(state.review)reviewDrafts.set(state.review,reviewDraft());}
function openReview(id) {
  if(reviewSaving){$('review-question').value=state.review;return;}
  const run=state.runs.find(r=>r.id===id);if(!run)return;
  if($('review-dialog').open)stashReviewDraft();
  state.review=id;state.current=id;
  const draft=reviewDrafts.get(id)||run.review||{};
  $('correctness').value=draft.correctness||'';$('citation-support').value=draft.citation_support||'';
  $('reviewer').value=draft.reviewer||'';$('review-notes').value=draft.notes||'';
  const queue=reviewQueue(run),index=queue.findIndex(r=>r.id===id);
  $('review-question').replaceChildren(...queue.map((r,i)=>new Option(
    (i+1)+' · '+(r.request.question_id||'自由问题')+' · '+(reviewComplete(r)?'已评审':'待评审')+' · '+r.message,r.id)));
  $('review-question').value=id;
  $('review-progress').textContent=(run.batch_id?'本轮批次':'非批次记录')+' · 第 '+(index+1)+' / '+queue.length+' 条 · 完整评审 '+queue.filter(reviewComplete).length+' 条';
  $('review-previous').disabled=index===0;$('review-next').disabled=index===queue.length-1;
  $('review-save-next').textContent=index<queue.length-1?'保存并评审下一条':'保存并结束';
  const ref=$('review-reference');ref.replaceChildren(el('p','field-help','RUN '+run.number+' · '+(run.request.question_id||'自由问题')),el('h3','',run.message));
  const answer=el('section','review-answer');answer.append(el('h4','','本次实际回答'),el('p','',run.response.answer));ref.append(answer);
  const points=run.response.evaluation.reference_points;
  if(points?.length){const d=el('details');d.open=true;d.append(el('summary','','参考答案要点'));const list=el('ul');for(const point of points)list.append(el('li','',point));d.append(list);ref.append(d);}
  else ref.append(el('p','field-help','此问题没有预置标准答案，需要自行核对事实和引用。'));
  const sources=el('details','review-sources');sources.append(el('summary','','本次引用与入模证据'));
  const citations=run.response.citations||[],evidence=run.response.retrieval_trace?.stages?.evidence||[];
  if(!citations.length)sources.append(el('p','field-help','本次回答未提供引用。'));
  for(const c of citations){const row=el('p','field-help');row.append(el('span','',(c.title||c.source||'引用')+' · '+(c.version||'')));
    if(c.chunk_id&&c.chunk_set_id)row.append(button('核对原文',()=>openSource(c),'text-button'));sources.append(row);}
  for(const hit of evidence){const item=el('article','review-evidence');item.append(el('strong','',hit.citation?.title||hit.source||'证据'),el('p','',hit.text||''));sources.append(item);}
  ref.append(sources);$('review-save-status').textContent='';
  if(!$('review-dialog').open)$('review-dialog').showModal();
}
function moveReview(offset){
  const run=state.runs.find(r=>r.id===state.review);if(!run)return;
  const queue=reviewQueue(run),next=queue[queue.findIndex(r=>r.id===run.id)+offset];if(next)openReview(next.id);
}
async function saveReview(event){
  event.preventDefault();
  if(reviewSaving||!$('review-form').reportValidity())return;
  const run=state.runs.find(r=>r.id===state.review);if(!run)return;
  const queue=reviewQueue(run),next=queue[queue.findIndex(r=>r.id===run.id)+1],advance=event.submitter?.id==='review-save-next';
  if(!$('reviewer').value.trim()||!$('review-notes').value.trim())return;
  run.review={correctness:$('correctness').value||null,citation_support:$('citation-support').value||null,reviewer:$('reviewer').value.trim(),notes:$('review-notes').value.trim(),reviewed_at:new Date().toISOString(),experiment_id:run.id};
  stashReviewDraft();renderExperiments();if(state.current===run.id)renderMetrics(run);
  $('review-save').disabled=$('review-save-next').disabled=true;$('review-save-status').textContent='正在保存…';
  reviewSaving=true;const epoch=state.epoch;let saved;
  try{saved=await saveCache();}finally{reviewSaving=false;}
  if(epoch!==state.epoch)return;
  $('review-save').disabled=$('review-save-next').disabled=false;
  if(saved===false){$('review-save-status').textContent='本地保存失败，请重试或导出记录。';return;}
  reviewDrafts.delete(run.id);
  if(advance&&next){openReview(next.id);$('review-dialog').scrollTop=0;}
  else{$('review-dialog').close();state.review=null;showNotice(advance?'本轮评审已保存。':'本条评审已保存。');}
}
function exportRuns() {
  const payload={version:1,exported_at:new Date().toISOString(),identity:state.identity,
    scope:'browser-session snapshots; human labels are local; repeat runs are not independent questions',
    datasets:state.dataset?[state.dataset]:[],batches:state.batches,runs:state.runs,failed_requests:state.failures};
  $('export-json').value=JSON.stringify(payload,null,2);
  $('export-status').textContent=`${state.runs.length} 次运行、${state.failures.length} 次失败请求。可下载或复制完整 JSON；记录保存在当前浏览器中。`;
  $('export-dialog').showModal();
}
$('download-json').addEventListener('click',()=>{
  const blob=new Blob([$('export-json').value],{type:'application/json'}),url=URL.createObjectURL(blob),link=el('a');
  link.href=url;link.download=`knowledgeops-${state.identity}-${Date.now()}.json`;
  document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),10000);
  $('export-status').textContent='已发起下载。如果当前浏览器没有保存文件，可复制下方 JSON。';
});
$('copy-json').addEventListener('click',async()=>{
  try{await navigator.clipboard.writeText($('export-json').value);$('export-status').textContent='完整 JSON 已复制。';}
  catch{$('export-json').focus();$('export-json').select();$('export-status').textContent='内容已选中，请按 Ctrl+C 复制。';}
});
async function runBatch(resuming=false) {
  resuming=resuming===true;
  if(state.batchRunning){state.batchStop=true;showNotice('将完成当前题目，随后停止后续调用。');return;}
  const err=validateControls();if(err){showNotice(err,true);return;}if(!state.dataset||!state.evaluationMode)return;
  const previous=resuming?state.batches.at(-1):null;
  if(resuming&&(!previous||!state.datasetUpload))return;
  state.importBusy=true;updateControls();
  try{if(cacheScope&&cacheScope!==cache.scopeKey(state.identity,await api('/workbench/connections')))throw new Error('知识库连接已变化，请刷新页面加载对应记录。');if(state.datasetUpload)state.dataset=await api('/workbench/datasets',{method:'POST',body:JSON.stringify(state.datasetUpload)});}
  catch(e){showNotice(e.message,true);return;}finally{state.importBusy=false;updateControls();}
  const dataset=state.dataset, selectedSplit=previous?previous.split==='all'?'':previous.split:$('dataset-split').value;
  const questions=dataset.questions.filter(q=>!selectedSplit||q.split===selectedSplit), frozenControls=previous?previous.controls:structuredClone(controls());
  state.batchRunning=true;state.batchStop=false;state.busy=true;updateControls();
  const batch=previous||{id:crypto.randomUUID(),dataset:{name:dataset.name,fingerprint:dataset.fingerprint},split:selectedSplit||'all',identity:state.identity,controls:frozenControls,started:new Date().toISOString(),planned:questions.length,completed:0,errors:0,status:'running'};
  if(!previous)state.batches.push(batch);batch.status='running';state.activeBatch=batch;
  try {
    const ready=await api('/workbench/readiness?semantic='+frozenControls.retrieval.semantic);
    if(!ready.ready)throw new Error('评测尚未开始：'+ready.reasons.join('；')+'。请前往连接设置或知识库。');
    const runtime=(await api('/workbench/datasets/runtime')).fingerprint;if(previous&&batch.runtime_fingerprint&&batch.runtime_fingerprint!==runtime)throw new Error('运行配置已变化，请恢复原配置后继续，或开始新批次。');batch.runtime_fingerprint=runtime;await saveCache();
    for(const q of cache.remainingQuestions(questions,batch.id,state.runs,state.failures)){
      if(state.batchStop)break;
      $('dataset-progress').textContent=`${batch.completed+batch.errors+1} / ${questions.length} · ${q.question_id} · 各题独立运行`;
      batch.inFlight=q.question_id;await saveCache();
      try{await runChat(q.question,{fresh:true,batch:true,questionId:q.question_id,dataset,frozenControls,runtime,batchId:batch.id});batch.completed++;}
      catch(error){batch.errors++;if(['RUNTIME_CHANGED','DATASET_NOT_FOUND','MODEL_NOT_CONFIGURED','KNOWLEDGE_NOT_READY','WORKSPACE_BUSY'].includes(error.code)){batch.status='failed';showNotice(error.message,true);break;}}
      batch.inFlight=null;await saveCache();
    }
    if(batch.status==='running')batch.status=state.batchStop?'stopped':'completed';
  } catch(error){batch.status='failed';batch.error=error.message;showNotice(error.message,true);}
  finally {
    batch.finished=new Date().toISOString();state.batchRunning=false;state.busy=false;state.activeBatch=null;
    $('dataset-progress').textContent=`${batch.status==='stopped'?'已停止':batch.status==='failed'?'已中止':'已完成'} · ${batch.completed} 次响应 / ${batch.errors} 次失败 / ${batch.planned-batch.completed-batch.errors} 题未运行`;
    updateControls();renderExperiments();await saveCache();
  }
}
function renderAgent(agent) {
  let box=document.getElementById('agent-'+agent.run_id);
  if(!box){box=el('article','agent-card');box.id='agent-'+agent.run_id;$('chat-log').append(box);}
  const expanded=new Set([...box.querySelectorAll('details[open][data-detail-key]')].map(n=>n.dataset.detailKey));
  box.replaceChildren();box.dataset.status=agent.status;
  const pending=currentProposal(agent),header=el('div','agent-header');
  const status=el('span','agent-status',pending?(pending.expires_at*1000<=Date.now()?'预览已过期':'待你确认'):agent.outcome==='needs_clarification'?'需要补充信息':statusNames[agent.status]||agent.status);
  status.dataset.status=agent.status;header.append(messageLabel('assistant'),status);box.append(header);
  const layout=el('div','agent-layout'),content=el('div','agent-content'),sidebar=el('aside','agent-sidebar');
  sidebar.setAttribute('aria-label','任务处理记录');layout.append(content,sidebar);box.append(layout);
  sidebar.append(progressView(agent,openSource));
  if(agent.answer)content.append(el('p','agent-answer',agent.answer));
  const receipts=(agent.trace||[]).filter(t=>t.name==='create_ticket'&&t.status==='committed'&&t.result?.ticket_id);
  for(const trace of receipts)content.append(receiptView(trace,agent.events));
  if(pending){
    const p=pending;
    content.append(proposalView(p,{
      approve:approved=>{const decision=proposalDecision(p,approved);return agentAction(decision.suffix,decision.body,agent.run_id);},
      revise:arguments_=>agentAction('proposal',{action_id:p.action_id,version:p.version,digest:p.digest,arguments:arguments_},agent.run_id)
    }));
  }
  if(agent.status==='cancelled'){
    content.append(el('p','ticket-rejected',agent.outcome==='rejected'?'已拒绝本次提案，本次提案未创建工单。':receipts.length?'后续任务已取消；已创建的工单仍可查看。':'任务已取消，未创建工单。'));
  }
  if(agent.status==='failed'){
    content.append(el('p','error-text',receipts.length?'工单已创建，后续回复失败。可查看工单详情，无需重复提交。':agentFailureMessage(agent.outcome)));
    if(agent.outcome==='UNPRICED_TOOL'){const link=el('a','secondary-button','填写 Embedding 单价');link.href='/settings#embedding-section';content.append(link);}
  }
  if(!content.childElementCount)content.append(el('p','agent-lead',agent.status==='running'?'正在核对信息，处理记录会持续更新。':agent.outcome==='needs_clarification'?'请补充所需信息，再继续处理。':'本次任务已结束，可查看处理记录。'));
  const actions=el('div','actions agent-utilities');
  if(['running','waiting_approval','interrupted'].includes(agent.status))actions.append(button('刷新任务',()=>{if(state.agent?.run_id===agent.run_id)refreshAgent();},'secondary-button'));
  if(['running','interrupted'].includes(agent.status))actions.append(button('取消任务',()=>agentAction('cancel',undefined,agent.run_id),'secondary-button danger-button'));
  if(agent.status==='interrupted')actions.append(button('恢复执行',()=>agentAction('resume',undefined,agent.run_id),'secondary-button'));
  sidebar.append(actions);
  const events=el('details','agent-diagnostics');events.dataset.detailKey='diagnostics';
  events.append(el('summary','','任务详情与事件'),el('p','field-help','任务编号 '+agent.run_id),el('p','field-help',(agent.model||'Agent')+' · 已执行 '+agent.steps+' 步'));
  for(const event of agent.events||[])events.append(el('p','field-help','#'+event.id+' · '+event.type));
  sidebar.append(events);
  {$('inspection-answer').hidden=true;const metrics=$('metrics-panel');metrics.replaceChildren();$('run-number').textContent='AGENT';const g=group('Agent 运行');g.append(line('状态',agent.outcome==='needs_clarification'?'需要补充信息':statusNames[agent.status]||agent.status),line('执行耗时',ms((agent.elapsed_seconds||0)*1000)),line('模型 tokens',agent.actual_model_tokens??'未知'),line('估算模型费用',agent.estimated_model_cost??'未知'),line('执行步骤',agent.steps),el('p','metric-caption','任务使用已配置模型与执行预算；耗时不包含人工审批等待。'));metrics.append(g);$('evidence-list').className='empty-state';$('evidence-list').textContent='Agent 工具结果与来源在任务轨迹中展示。';}for(const detail of box.querySelectorAll('details[data-detail-key]'))if(expanded.has(detail.dataset.detailKey))detail.open=true;updateControls();
}
async function pollAgent(id,identity,epoch) {
  for(let i=0;i<180;i++){if(epoch!==state.epoch||state.agent?.run_id!==id)return;await new Promise(resolve=>setTimeout(resolve,500));try {const agent=await api(`/agent/${encodeURIComponent(id)}`,{},identity);if(epoch!==state.epoch||state.agent?.run_id!==id)return;state.agent=agent;state.agentByIdentity.set(identity,agent);renderAgent(agent);if(agent.status!=='running'){$('chat-log').scrollTop=$('chat-log').scrollHeight;return;}}catch(error){showNotice(error.message+' 可手动刷新任务。',true);return;}}
  showNotice('任务仍在运行，可以刷新状态或取消任务。');
}
async function agentAction(suffix,body,expectedRunId=state.agent?.run_id) {
  const agent=state.agent;if(!agent||agent.run_id!==expectedRunId){showNotice('这是较早任务的记录，请操作当前待审批提案。',true);return null;}
  if(state.busy)return null;
  const epoch=state.epoch,identity=state.identity;
  const buttons=[...document.querySelectorAll('.agent-card button')].map(node=>({node,disabled:node.disabled}));
  state.busy=true;updateControls();for(const item of buttons)item.node.disabled=true;
  try{
    let updated;
    try{
      updated=await api('/agent/'+encodeURIComponent(agent.run_id)+'/'+suffix,{method:'POST',...(body?{body:JSON.stringify(body)}:{})},identity);
    }catch(error){
      // A refusal may cross the expiry boundary in transit. Cancel only this user-requested refusal.
      if(error.code!=='APPROVAL_EXPIRED'||suffix!=='approval'||body?.approve!==false)throw error;
      updated=await api('/agent/'+encodeURIComponent(agent.run_id)+'/cancel',{method:'POST'},identity);
    }
    if(epoch!==state.epoch)return null;
    state.agent=updated;state.agentByIdentity.set(identity,updated);renderAgent(updated);
    if(updated.status==='cancelled'&&agent.pending?.expires_at*1000<=Date.now())showNotice('过期预览已关闭，未创建工单。可以重新发起任务。');
    if(suffix==='proposal'){showNotice('修改已保存，请核对新版本后确认创建。');document.getElementById('agent-'+agent.run_id)?.querySelector('.proposal-actions .primary-button')?.focus();}
    if(updated.status==='running')pollAgent(updated.run_id,identity,epoch);
    return updated;
  }catch(error){
    showNotice(error.code==='APPROVAL_EXPIRED'?'工单预览已过期，不能继续创建或修改。请关闭过期预览后重新发起任务。':error.message,true);
    if(epoch===state.epoch&&['APPROVAL_MISMATCH','INVALID_STATE','APPROVAL_EXPIRED'].includes(error.code))await refreshAgent();
    return null;
  }finally{
    if(epoch===state.epoch){state.busy=false;for(const item of buttons)if(item.node.isConnected)item.node.disabled=item.disabled;updateControls();}
  }
}
async function refreshAgent(){if(!state.agent)return;const epoch=state.epoch,id=state.agent.run_id;try{const a=await api(`/agent/${encodeURIComponent(id)}`);if(epoch!==state.epoch||state.agent?.run_id!==id)return;state.agent=a;state.agentByIdentity.set(state.identity,a);renderAgent(a);}catch(error){showNotice(error.message,true);}}
$('parameters-form').addEventListener('invalid',event=>{let group=event.target.closest('details');while(group){group.open=true;group=group.parentElement.closest('details');}},true);
$('parameters-form').addEventListener('input',updateControls);$('parameters-form').addEventListener('change',updateControls);$('reset-config').addEventListener('click',applyDefaults);

$('question').addEventListener('change',()=>{if($('question').value==='import'){openDatasetDialog();renderQuestions();}else{state.evaluationMode=$('question').value==='dataset';updateControls();}});

$('dismiss-notice').addEventListener('click',()=>{$('notice').hidden=true;});
$('message').addEventListener('input',resizeComposer);
window.addEventListener('resize',resizeComposer);
$('message').addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();if(!$('send').disabled)$('composer').requestSubmit();}});
$('composer').addEventListener('submit',async event=>{event.preventDefault();const text=$('message').value.trim();if(!text||state.busy)return;try{await runChat(text,{unified:true});}catch{}});
$('new-conversation').addEventListener('click',()=>{state.conversation=null;state.agent=null;state.agentRequest=null;state.evaluationMode=false;renderQuestions();updateControls();showWelcome();$('composer-note').textContent='新会话 · Enter 发送';$('message').focus();});
$('chat-log').addEventListener('click',event=>{
  const b=event.target.closest('[data-prompt]');if(!b||!$('chat-log').contains(b)||state.busy)return;
  state.evaluationMode=false;state.view='chat';$('intent').value=b.dataset.mode||'auto';
  renderQuestions();updateControls();$('message').value=b.dataset.prompt;resizeComposer();$('message').focus();
});

for(const b of document.querySelectorAll('[data-agent]'))b.addEventListener('click',()=>{$('message').value=b.dataset.agent;resizeComposer();$('message').focus();});
$('metrics-tab').addEventListener('click',()=>inspectorTab('metrics'));$('evidence-tab').addEventListener('click',()=>inspectorTab('evidence'));
function inspectorTab(name){$('metrics-panel').hidden=name!=='metrics';$('evidence-panel').hidden=name!=='evidence';for(const n of ['metrics','evidence']){$(`${n}-tab`).classList.toggle('active',name===n);$(`${n}-tab`).setAttribute('aria-pressed',String(name===n));}}
$('evidence-stage').addEventListener('change',()=>renderEvidence(state.runs.find(r=>r.id===state.current)));
$('rerun').addEventListener('click',async()=>{if(!state.last)return;try{if(state.last.dataset&&state.datasetUpload&&state.last.dataset.fingerprint===state.dataset?.fingerprint){state.last.dataset=await api('/workbench/datasets',{method:'POST',body:JSON.stringify(state.datasetUpload)});}await runChat(state.last.message,{fresh:true,questionId:state.last.questionId,dataset:state.last.dataset});}catch{}});
$('compare').addEventListener('click',compareRuns);
$('comparison-baseline').addEventListener('change',updateComparisonCandidates);
$('comparison-same-question').addEventListener('change',updateComparisonCandidates);
$('comparison-candidate').addEventListener('change',()=>{state.b=$('comparison-candidate').value;renderComparison();scheduleSave();});$('batch').addEventListener('click',runBatch);$('export').addEventListener('click',exportRuns);
for(const b of document.querySelectorAll('[data-close]'))b.addEventListener('click',()=>$(b.dataset.close).close());
$('review-form').addEventListener('submit',saveReview);
$('review-previous').addEventListener('click',()=>moveReview(-1));
$('review-next').addEventListener('click',()=>moveReview(1));
$('review-question').addEventListener('change',()=>openReview($('review-question').value));
$('review-dialog').addEventListener('close',()=>{if(state.review)stashReviewDraft();});

$('identity').addEventListener('change',async()=>{await saveCache();cacheReady=false;cacheScope=null;state.datasetUpload=null;state.identity=$('identity').value;try{sessionStorage.setItem('knowledgeops-identity',state.identity);}catch{}state.epoch++;reviewDrafts.clear();state.dataset=null;state.evaluationMode=false;state.batches=[];state.conversation=null;state.runs=[];state.failures=[];state.current=null;state.a=null;state.b=null;state.last=null;state.review=null;state.agent=state.agentByIdentity.get(state.identity)||null;state.agentRequest=null;state.config=null;$('message').value='';for(const d of document.querySelectorAll('dialog'))if(d.open)d.close();showWelcome();$('inspection-answer').hidden=true;$('inspection-answer').replaceChildren();$('metrics-panel').replaceChildren(el('div','empty-state','当前身份还没有问答实验。'));$('run-number').textContent='—';renderEvidence(null);renderExperiments();updateControls();await loadConfig();await restoreCache();if(state.agent){state.conversation=state.agent.conversation_id;renderAgent(state.agent);await refreshAgent();if(state.agent.status==='running')pollAgent(state.agent.run_id,state.identity,state.epoch);}});
function renderPipeline(){
  const box=$('pipeline-map');box.replaceChildren();
  const branches=el('div','pipeline-branches');
  for(const [id,label]of [['semantic','语义'],['keyword','关键词']]){const n=el('span',$ (id).checked?'pipeline-node':'pipeline-node off',`${label} ${$(id).checked?$(id+'-k').value:'关闭'}`);branches.append(n);$(id+'-k').closest('label').hidden=!$(id).checked;}
  box.append(branches,el('span','pipeline-arrow','↓'),el('div','pipeline-node',($('semantic').checked&&$('keyword').checked)?`RRF · 保留 ${$('fusion-k').value}`:'单路直通'),el('span','pipeline-arrow','↓'),el('div','pipeline-node final-node',`证据 ≤ ${$('evidence-k').value} 切块`));
  $('fusion-k').closest('label').hidden=!($('semantic').checked&&$('keyword').checked);
  for(const id of ['temperature','top-p']){const range=$(id+'-range');if(range)range.value=$(id).value||1;}
}
function preset(name){
  if(name==='custom')return;
  $('semantic').checked=true;$('keyword').checked=name!=='fast';$('rrf').checked=true;
  $('profile').value=name==='cross'?'multi_query':'ranked';$('coverage-review').checked=name!=='fast';
  for(const id of ['semantic-k','keyword-k','fusion-k'])$(id).value=name==='fast'?10:name==='cross'?30:20;
  $('evidence-k').value=name==='fast'?4:name==='cross'?12:8;
  $('preset-status').textContent={fast:'快速问答',balanced:'均衡检索',cross:'跨文档分析'}[name];updateControls();
}
$('preset').addEventListener('change',()=>preset($('preset').value));
$('parameters-form').addEventListener('input',()=>{$('preset').value='custom';$('preset-status').textContent='已自定义';});
$('reset-config').addEventListener('click',()=>{$('preset').value='custom';$('preset-status').textContent='服务端默认';});
for(const id of ['temperature','top-p']){const range=el('input');range.type='range';range.id=id+'-range';range.min=$(id).min;range.max=$(id).max;range.step=$(id).step;range.setAttribute('aria-label',id==='temperature'?'Temperature 滑块':'Top-P 滑块');$(id).closest('label').after(range);range.addEventListener('input',()=>{$(id).value=range.value;$(id).dispatchEvent(new Event('input',{bubbles:true}));});}
function showParameters() { if(!$('parameters-dialog').open)$('parameters-dialog').showModal(); }
function showInspector() { if(!$('inspector-dialog').open)$('inspector-dialog').showModal(); }
function setWorkspaceView(view) {
  if(state.busy||state.importBusy)return;
  state.view=view;
  if(view==='chat'&&!state.agent&&!$('chat-log').childElementCount)showWelcome();
  state.evaluationMode=view==='evaluation'&&!!state.dataset;
  renderQuestions();updateControls();scheduleSave();
}
function renderWorkspaceView() {
  $('chat-view').hidden=state.view!=='chat';
  $('evaluation-view').hidden=state.view!=='evaluation';
  for(const name of ['chat','evaluation']) {
    $(name+'-view-button').setAttribute('aria-pressed',String(state.view===name));
    $(name+'-view-button').disabled=state.busy||state.importBusy;
  }
}
$('toggle-parameters').addEventListener('click',showParameters);
$('open-inspector').addEventListener('click',showInspector);
$('chat-view-button').addEventListener('click',()=>setWorkspaceView('chat'));
$('evaluation-view-button').addEventListener('click',()=>setWorkspaceView('evaluation'));
$('model-settings').addEventListener('click',async()=>{try{const c=await api('/workbench/model');$('base-url').value=c.base_url;$('model-id').value=c.model_id;$('api-key').value='';$('api-key').placeholder=c.key_present?'已设置 · 留空保留现有密钥':'输入 API Key';$('input-price').value=c.input_price??'';$('output-price').value=c.output_price??'';$('model-status').textContent=c.agent_ready?'问答与 Agent 已连接':c.configured?'问答已连接；填写两项单价可启用 Agent':'尚未连接模型';$('model-dialog').showModal();}catch(e){showNotice(e.message,true);}});
function modelBody(){return {base_url:$('base-url').value.trim(),model_id:$('model-id').value.trim(),...($('api-key').value?{api_key:$('api-key').value}:{}),input_price:$('input-price').value===''?null:Number($('input-price').value),output_price:$('output-price').value===''?null:Number($('output-price').value)};}
async function configureModel(test){if(!$('model-form').reportValidity())return;for(const id of ['test-model','save-model'])$(id).disabled=true;$('model-status').textContent=test?'正在测试连接…':'正在保存…';try{const result=await api(test?'/workbench/model/test':'/workbench/model',{method:test?'POST':'PUT',body:JSON.stringify(modelBody())});$('model-status').textContent=test?result.message:'已保存到服务端内存。';if(!test){$('api-key').value='';await loadConfig(false);applyDefaults();$('model-dialog').close();showNotice(result.agent_ready?'真实模型已启用，问答与任务共用当前模型。':'真实问答模型已启用。填写模型单价后可执行 Agent 任务。');}}catch(e){$('model-status').textContent='连接或保存失败：'+e.message+'。请检查地址、模型名称、密钥和网络。';}finally{for(const id of ['test-model','save-model'])$(id).disabled=false;}}
$('test-model').addEventListener('click',()=>configureModel(true));$('model-form').addEventListener('submit',e=>{e.preventDefault();configureModel(false);});
$('model-dialog').addEventListener('close',()=>{$('api-key').value='';});
function openDatasetDialog(){if(state.busy)return;$('dataset-import-status').textContent='';$('dataset-dialog').showModal();}
$('welcome-import').addEventListener('click',openDatasetDialog);$('replace-dataset').addEventListener('click',openDatasetDialog);
$('dataset-split').addEventListener('change',()=>{const count=state.dataset.questions.filter(q=>!$('dataset-split').value||q.split===$('dataset-split').value).length;$('dataset-progress').textContent=`待运行 ${count} 题 · 参数将在开始时固定`;});
$('start-dataset').addEventListener('click',runBatch);
$('dataset-form').addEventListener('submit',async event=>{
  event.preventDefault();const file=$('dataset-file').files[0],spans=$('spans-file').files[0];if(!file)return;
  if([file,spans].filter(Boolean).some(f=>!f.name.toLowerCase().endsWith('.jsonl')||f.size>2000000)){$('dataset-import-status').textContent='请选择不超过 2 MB 的 .jsonl 文件。';return;}
  state.importBusy=true;$('confirm-dataset').disabled=true;updateControls();const epoch=state.epoch;
  $('dataset-import-status').textContent='正在校验格式和标注…';
  try{const upload={filename:file.name,content:await file.text(),spans_content:spans?await spans.text():''};const data=await api('/workbench/datasets',{method:'POST',body:JSON.stringify(upload)});state.datasetUpload=upload;if(epoch!==state.epoch)return;
    state.dataset=data;state.view="evaluation";state.evaluationMode=true;renderQuestions();$('dataset-name').textContent=data.name;
    $('dataset-summary').textContent=`${data.count} 题可运行 · ${data.source_labels} 题有来源标注 · ${data.span_labels} 题有章节坐标 · ${data.answer_labels} 题有答案要点${data.excluded?` · ${data.excluded} 题不适用当前身份`:''}。导入不会上传知识库原文。`;
    const all=el('option','','全部题目');all.value='';$('dataset-split').replaceChildren(all);
    for(const [split,count]of Object.entries(data.splits)){const o=el('option','',`${split} · ${count} 题`);o.value=split;$('dataset-split').append(o);}
    $('dataset-progress').textContent=`已导入，尚未运行。${Object.keys(data.splits).length>1?'包含多个数据划分，可选择测试范围。':''}`;
    $('dataset-dialog').close();showNotice('测试集已校验。检查参数后点击“开始评测”，各题使用独立上下文。');
  }catch(error){$('dataset-import-status').textContent=error.message;}
  finally{state.importBusy=false;$('confirm-dataset').disabled=false;updateControls();scheduleSave();}
});
$('example-dataset').addEventListener('click',async()=>{try{const data=await api('/workbench/datasets/example');const url=URL.createObjectURL(new Blob([data.content],{type:'application/x-ndjson'})),a=el('a');a.href=url;a.download=data.filename;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),10000);$('dataset-import-status').textContent='已发起下载；示例为当前身份可用的开发集，包含内嵌章节坐标。';}catch(e){$('dataset-import-status').textContent=e.message;}});
state.importBusy=true;updateControls();loadConfig().then(async()=>{await restoreCache();applyBusinessPrompt();});


function snapshotCache(){
  const fields=Object.fromEntries(savedFields.map(id=>[id,$(id).type==='checkbox'?$(id).checked:$(id).value]));
  return structuredClone({version:1,dataset:state.dataset,datasetUpload:state.datasetUpload,evaluationMode:state.evaluationMode,batches:state.batches,runs:state.runs,failures:state.failures,current:state.current,a:state.a,b:state.b,last:state.last,fields});
}
function scheduleSave(){if(!cacheReady)return;clearTimeout(cacheTimer);cacheTimer=setTimeout(saveCache,150);}
function saveCache(){
  if(!cacheReady||!cacheScope)return Promise.resolve();clearTimeout(cacheTimer);
  const key=cacheScope,value=snapshotCache();$('cache-status').textContent='保存中…';
  cacheQueue=cacheQueue.catch(()=>{}).then(()=>cache.write(key,value)).then(()=>{$('cache-status').textContent='已保存在本机';return true;}).catch(error=>{$('cache-status').textContent='本地保存失败，请导出 JSON';showNotice('本地保存失败：'+error.message,true);return false;});
  return cacheQueue;
}
function restoreConversationRuns(){
  const chatRuns=state.runs.filter(r=>!r.batch_id&&r.view!=='evaluation');
  if(chatRuns.length){
    $('chat-log').replaceChildren();
    for(const run of chatRuns){message('user',run.message);message('assistant',run.response.answer,run);}
  }else if(!state.agent){
    showWelcome();
  }
  if(state.agent)renderAgent(state.agent);
}
async function restoreCache(){
  cacheReady=false;
  try{
    cacheScope=cache.scopeKey(state.identity,await api('/workbench/connections'));
    const stored=await cache.read(cacheScope);
    if(stored?.version===1){
      const data=cache.recover(stored);for(const k of ['dataset','datasetUpload','evaluationMode','batches','runs','failures','current','a','b','last'])state[k]=data[k];
      state.view=state.evaluationMode?"evaluation":"chat";renderQuestions();
      if(state.dataset){const d=state.dataset;$('dataset-name').textContent=d.name;$('dataset-summary').textContent=`已恢复测试集 · ${d.count} 题 · ${d.source_labels} 题有来源标注`;$('dataset-split').replaceChildren(new Option('全部题目',''),...Object.entries(d.splits).map(([s,n])=>new Option(`${s} · ${n} 题`,s)));}
      for(const [id,value]of Object.entries(data.fields||{}))if(savedFields.includes(id)){if($(id).type==='checkbox')$(id).checked=value;else $(id).value=value;}
      restoreConversationRuns();
      if(state.runs.length)selectRun(state.runs.some(r=>r.id===state.current)?state.current:state.runs.at(-1).id);
      const batch=state.batches.at(-1);if(batch&&batch.status!=='completed')$('dataset-progress').textContent='批次已中断。点击“继续中断批次”手动恢复；未返回结果的题目可能再次调用模型。';
      if(state.runs.length||state.batches.length)showNotice(`已恢复 ${state.runs.length} 条运行记录与评审，可在「评测与记录」中查看。`);
    }
    cacheReady=true;renderExperiments();updateControls();
  }catch(error){$('cache-status').textContent='本地缓存不可用';showNotice('无法恢复本地记录：'+error.message,true);}
  finally{state.importBusy=false;updateControls();}
}
$('resume-batch').onclick=()=>runBatch(true);
$('clear-cache').onclick=async()=>{
  if(!confirm('清空当前身份、数据库和集合的本地测试集、评测记录与评审？'))return;
  cacheReady=false;clearTimeout(cacheTimer);await cacheQueue;
  try{await cache.remove(cacheScope);location.reload();}catch(e){cacheReady=true;showNotice('清空失败：'+e.message,true);}
};
for(const id of ['parameters-form','question','dataset-split'])$(id).addEventListener('change',scheduleSave);
$('parameters-form').addEventListener('input',scheduleSave);
for(const id of ['reset-config','preset','new-conversation'])$(id).addEventListener('click',scheduleSave);
for(const a of document.querySelectorAll('.workspace-nav a'))a.addEventListener('click',async event=>{if(event.ctrlKey||event.metaKey||event.shiftKey||event.button)return;event.preventDefault();if(await saveCache()===false)return;location.assign(a.href);});
window.addEventListener('pagehide',()=>{saveCache();});
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='hidden')saveCache();});

window.addEventListener('pageshow',event=>{if(event.persisted)location.reload();});

// Only fixed prompts are accepted from the business page; never auto-submit.
const businessPrompts={
  device:'查询我本人的模拟设备状态，结合公司知识库解释合规问题并给出处理建议；如需 IT 协助，先整理工单供我确认。',
  account:'查询我本人的模拟账号安全状态，结合 OneGate 政策核对 MFA 和临时权限要求；不要修改账号或授予权限。',
  tickets:'查询我最近提交的本地模拟工单，说明当前状态。没有处理人或预计完成时间时明确说明未知，不推测。',
  durations:'查询本人的独立工单耗时演示记录，调用计算工具求总耗时并与目标比较；不要把它当作新建工单的实时进度。'
};
const businessTask=new URLSearchParams(location.search).get('business_task');
function applyBusinessPrompt(){
  if(!businessPrompts[businessTask])return;
  setWorkspaceView('chat');$('intent').value='auto';$('message').value=businessPrompts[businessTask];resizeComposer();$('message').focus();
  showNotice('已填入业务分析任务，点击发送后才会调用 Agent。');
}
