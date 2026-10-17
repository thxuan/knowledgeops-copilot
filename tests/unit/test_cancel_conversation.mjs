import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
const source=await readFile(new URL('../../app/web/workbench.js',import.meta.url),'utf8');
const render=source.slice(source.indexOf('function clearCancelledConversation('),source.indexOf('async function pollAgent('));
const action=source.slice(source.indexOf('async function agentAction('),source.indexOf('async function refreshAgent('));

function harness(responses=[]){
 const nodes=new Map(),calls=[],saved=[],notices=[];
 const node=id=>{if(!nodes.has(id))nodes.set(id,{value:'未发送草稿',hidden:false,open:false,children:[],replaceChildren(...items){this.children=items;},focus(){this.focused=true;},close(){this.open=false;}});return nodes.get(id);};
 const agent={run_id:'active',status:'waiting_approval',pending:{expires_at:0},trace:[]};
 const record={id:'active',record_meta:{session_id:'original'},agent};
 const state={agent,agentRuns:[record],runs:[{id:'question'}],failures:[{id:'failure'}],identity:'engineer',agentByIdentity:new Map([['engineer',agent]]),conversation:'old-server-conversation',sessionId:'original',agentRequest:{key:'old-request'},current:'question',last:{message:'old question'},view:'chat',evaluationMode:false,busy:false,epoch:1};
 let welcomed=0,evidence='old',refreshed=0;
 const context=vm.createContext({state,crypto:globalThis.crypto,structuredClone,Date,JSON,encodeURIComponent,$:node,
  document:{querySelectorAll:()=>[]},el:(tag,cls,text)=>({tag,cls,text}),
  showWelcome(){welcomed++;node('chat-log').replaceChildren('welcome');},showNotice:text=>notices.push(text),
  renderQuestions(){},renderExperiments(){},updateControls(){},resizeComposer(){},
  renderEvidence:value=>{evidence=value;},scheduleSave(){saved.push({sessionId:state.sessionId,conversation:state.conversation,agent:state.agent});},
  api:async(path,options)=>{calls.push({path,options});const response=responses.shift();if(response instanceof Error)throw response;return response;},
  refreshAgent:async()=>{refreshed++;},pollAgent(){throw new Error('A cancelled task cannot poll again');}
 });
 vm.runInContext(render+action,context);
 return {context,state,record,node,calls,saved,notices,get welcomed(){return welcomed;},get evidence(){return evidence;},get refreshed(){return refreshed;}};
}

test('successful cancellation clears messages and draft, detaches history, and preserves records',async()=>{
 const cancelled={run_id:'active',status:'cancelled',trace:[]},env=harness([cancelled]);
 env.node('source-dialog').open=true;env.node('inspector-dialog').open=true;
 await vm.runInContext("agentAction('cancel',undefined,'active')",env.context);
 assert.deepEqual(env.calls.map(call=>call.path),['/agent/active/cancel']);
 assert.deepEqual(env.node('chat-log').children,['welcome']);assert.equal(env.node('message').value,'');
 assert.equal(env.state.agent,null);assert.equal(env.state.conversation,null);assert.notEqual(env.state.sessionId,'original');
 assert.equal(env.state.agentRequest,null);assert.equal(env.state.last,null);assert.equal(env.state.current,null);
 assert.equal(env.state.agentByIdentity.has('engineer'),false);assert.equal(env.state.busy,false);
 assert.equal(env.record.agent.status,'cancelled');assert.equal(env.state.agentRuns.length,1);assert.equal(env.state.runs.length,1);assert.equal(env.state.failures.length,1);
 assert.equal(env.node('inspection-answer').hidden,true);assert.equal(env.evidence,null);assert.equal(env.node('source-dialog').open,false);
 assert.equal(env.node('message').focused,true);assert.equal(env.saved.at(-1).conversation,null);assert.equal(env.saved.at(-1).agent,null);
});

test('closing an expired proposal clears the conversation after the cancellation fallback succeeds',async()=>{
 const expired=Object.assign(new Error('Expired'),{code:'APPROVAL_EXPIRED'});
 const env=harness([expired,{run_id:'active',status:'cancelled',trace:[]}]);
 await vm.runInContext("agentAction('approval',{approve:false},'active')",env.context);
 assert.deepEqual(env.calls.map(call=>call.path),['/agent/active/approval','/agent/active/cancel']);
 assert.equal(env.welcomed,1);assert.equal(env.state.agent,null);
});

test('failed cancellation leaves the existing conversation and draft untouched',async()=>{
 const env=harness([new Error('取消请求失败')]);
 await vm.runInContext("agentAction('cancel',undefined,'active')",env.context);
 assert.equal(env.welcomed,0);assert.equal(env.state.conversation,'old-server-conversation');assert.equal(env.node('message').value,'未发送草稿');
 assert.equal(env.state.agent.status,'waiting_approval');assert.equal(env.state.busy,false);
});

test('rendering an older cancelled record does not reset a newer active conversation',()=>{
 const env=harness();env.state.agentRuns.push({id:'old-task',agent:{status:'running'}});
 vm.runInContext("renderAgent({run_id:'old-task',status:'cancelled',trace:[]})",env.context);
 assert.equal(env.welcomed,0);assert.equal(env.state.agent.run_id,'active');assert.equal(env.state.sessionId,'original');
});

test('already-created ticket receipts stay in audit records after cancellation clears the view',()=>{
 const env=harness();env.state.agent={run_id:'active',status:'cancelled',trace:[{name:'create_ticket',status:'committed',result:{ticket_id:'existing-ticket'}}]};
 vm.runInContext('renderAgent(state.agent)',env.context);
 assert.equal(env.state.agent,null);assert.equal(env.record.agent.trace[0].result.ticket_id,'existing-ticket');assert.equal(env.welcomed,1);
});
