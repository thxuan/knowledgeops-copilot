import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const source=await readFile(new URL('../../app/web/experiment-cache.js',import.meta.url),'utf8');
const {scopeKey,recover,remainingQuestions}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
test('scope isolates identity, database, schema and collection without secrets',()=>{
 const c={database:{host:'localhost',port:55432,database:'kb',schema_name:'one',password:'secret'},embedding:{collection:'a',api_key:'key'}};
 const key=scopeKey('engineer',c);
 assert(!key.includes('secret'));assert(!key.includes('key'));
 for(const changed of [ {...c,database:{...c.database,schema_name:'two'}},{...c,database:{...c.database,database:'other'}},{...c,embedding:{collection:'b'}}])assert.notEqual(scopeKey('engineer',changed),key);
 assert.notEqual(scopeKey('employee',c),key);
});
test('recovery interrupts running batches and reconciles response checkpoint counts',()=>{
 const saved={batches:[{id:'a',status:'running',completed:0,errors:0},{id:'b',status:'completed'}],runs:[{batch_id:'a'}],failures:[{batch_id:'a'}]};
 const restored=recover(saved);
 assert.equal(restored.batches[0].status,'interrupted');assert.equal(restored.batches[0].completed,1);assert.equal(restored.batches[0].errors,1);
 assert.equal(restored.batches[1].status,'completed');assert.equal(saved.batches[0].status,'running');
});
test('manual continuation skips recorded successes and failures only in this batch',()=>{
 const questions=['q1','q2','q3','q4'].map(question_id=>({question_id}));
 const runs=[{batch_id:'current',request:{question_id:'q1'}},{batch_id:'old',request:{question_id:'q3'}}];
 const failures=[{batch_id:'current',request:{question_id:'q2'}}];
 assert.deepEqual(remainingQuestions(questions,'current',runs,failures).map(q=>q.question_id),['q3','q4']);
});

test('workbench boot restores interrupted cache without starting model requests',async()=>{
 const vm=await import('node:vm');
 const nodes=new Map();
 function element(id=''){
  if(nodes.has(id)&&id)return nodes.get(id);
  const n={id,value:id==='identity'?'u_engineer_001':'',type:'',checked:false,hidden:false,disabled:false,options:[],children:[],dataset:{},style:{},classList:{add(){},remove(){},toggle(){}},
   addEventListener(){},setAttribute(){},append(...xs){this.children.push(...xs);},replaceChildren(...xs){this.children=xs;},after(){},remove(){},closest(){return element();},checkValidity(){return true;},focus(){}};
  if(id)nodes.set(id,n);return n;
 }
 const saved={version:1,dataset:{id:'old',name:'test.jsonl',fingerprint:'f',count:1,source_labels:0,splits:{dev:1},questions:[{question_id:'q1',question:'test',split:'dev'}]},datasetUpload:{filename:'test.jsonl',content:'{}'},evaluationMode:true,batches:[{id:'b',status:'running',dataset:{fingerprint:'f'},completed:0,errors:0}],runs:[],failures:[],current:null,a:null,b:null,last:null,fields:{}};
 const calls=[];
 const config={generation:{ready:false,temperature:0,output_limit:2048,input_limit:32000,models:[],coverage_review:false},retrieval:{strategy:'vector',candidate_k:20,rrf_constant:60,rerank_available:false}};
 const connection={database:{host:'localhost',port:55432,database:'kb',schema_name:'test'},embedding:{collection:'c'}};
 const sandbox={console,structuredClone,AbortController,Event,crypto:globalThis.crypto,setTimeout:()=>0,clearTimeout(){},sessionStorage:{getItem(){return null;}},
  document:{getElementById:element,createElement:()=>element(),querySelectorAll:()=>[],addEventListener(){}},window:{addEventListener(){}},
  Option:function(text,value){this.textContent=text;this.value=value;},cache:{scopeKey,recover,remainingQuestions,read:async()=>saved,write:async()=>{}},
  fetch:async path=>{calls.push(path);return {ok:true,json:async()=>path==='/workbench/config'?config:path==='/workbench/tasks'?{tasks:[]}:connection};}};
 const script=(await readFile(new URL('../../app/web/workbench.js',import.meta.url),'utf8')).replace(/^import [^\n]*\n/,'');
 vm.runInNewContext(script,sandbox);
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(element('dataset-name').textContent,'test.jsonl');
 assert.equal(element('evaluation-view').hidden,false);
 assert.equal(element('chat-view').hidden,true);
 element('message').value='未发送的草稿';
 vm.runInNewContext("setWorkspaceView('chat')",sandbox);
 assert.equal(element('chat-view').hidden,false);
 assert.equal(element('composer').hidden,false);
 assert.equal(element('evaluation-view').hidden,true);
 vm.runInNewContext("setWorkspaceView('evaluation')",sandbox);
 assert.equal(element('dataset-panel').hidden,false);
 assert.equal(element('dataset-name').textContent,'test.jsonl');
 assert.equal(element('message').value,'未发送的草稿');
 vm.runInNewContext("state.busy=true;setWorkspaceView('chat')",sandbox);
 assert.equal(element('chat-view').hidden,true);
 vm.runInNewContext("state.busy=false",sandbox);

 assert.equal(element('resume-batch').hidden,false);
 assert.equal(element('resume-batch').disabled,false);
 assert(!calls.some(path=>path.includes('/chat')||path.includes('/message')||path==='/workbench/datasets'));
 // Evaluation responses belong to records; they must not alter a chat draft or transcript.
 sandbox.deliveredMessages=[];
 vm.runInNewContext(`
   validateControls=()=>''; controls=()=>({});
   renderMetrics=()=>{};renderEvidence=()=>{};renderExperiments=()=>{};
   message=(role,text)=>deliveredMessages.push({role,text});
   batchRequest=async()=>({experiment_id:'test-result',conversation_id:'conversation',answer:'test answer'});
 `,sandbox);
 await vm.runInNewContext("runChat('evaluation question',{batch:true,batchId:'batch-one'})",sandbox);
 assert.equal(sandbox.deliveredMessages.length,0);
 assert.equal(element('message').value,'未发送的草稿');
 assert.equal(vm.runInNewContext('state.runs.at(-1).view',sandbox),'evaluation');
 vm.runInNewContext("state.busy=false;setWorkspaceView('chat')",sandbox);
 await vm.runInNewContext("runChat('ordinary question')",sandbox);
 assert.deepEqual(sandbox.deliveredMessages.map(x=>x.role),['user','assistant']);
 assert.equal(element('message').value,'');

});
