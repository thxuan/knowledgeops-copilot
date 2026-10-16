import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
const view=await readFile(new URL('../../app/web/agent-view.js',import.meta.url),'utf8');
const {currentProposal,proposalDecision}=await import('data:text/javascript;base64,'+Buffer.from(view).toString('base64'));
const source=await readFile(new URL('../../app/web/workbench.js',import.meta.url),'utf8');
const actionCode=source.slice(source.indexOf('async function agentAction('),source.indexOf('async function refreshAgent('));
const proposal={action_id:'action',version:2,digest:'current-digest',expires_at:100};

test('closing an expired preview uses cancel without an approval payload',()=>{
  assert.deepEqual(proposalDecision(proposal,false,100000),{suffix:'cancel'});
});
test('a current refusal retains its version and digest binding',()=>{
  assert.deepEqual(proposalDecision(proposal,false,99000),{suffix:'approval',body:{
    action_id:'action',version:2,digest:'current-digest',approve:false}});
});
test('expiration never converts a creation request to another action',()=>{
  assert.equal(proposalDecision(proposal,true,200000).suffix,'approval');
});
test('terminal tasks cannot display a leftover proposal from older servers',()=>{
  for(const status of ['cancelled','completed','failed'])assert.equal(currentProposal({status,pending:proposal}),null);
  assert.equal(currentProposal({status:'waiting_approval',pending:proposal}),proposal);
});

function harness(responses){
  const agent={run_id:'current-run',status:'waiting_approval',pending:{...proposal,expires_at:0}};
  const state={agent,busy:false,identity:'engineer',epoch:1,agentByIdentity:new Map()};
  const calls=[],notices=[],rendered=[];
  let refreshes=0;
  const context=vm.createContext({state,Date,JSON,encodeURIComponent,
    document:{querySelectorAll:()=>[]},updateControls(){},
    api:async(path,options,identity)=>{
      calls.push({path,options,identity});
      const response=responses.shift();
      if(response instanceof Error)throw response;
      return response;
    },
    showNotice:(text,error)=>notices.push({text,error}),
    renderAgent:agent=>rendered.push(agent),refreshAgent:async()=>{refreshes++;},
    pollAgent(){throw new Error('Closing an expired task must not run the Agent');}
  });
  vm.runInContext(actionCode,context);
  return {context,state,calls,notices,rendered,get refreshes(){return refreshes;}};
}
const expired=()=>Object.assign(new Error('provider text'),{code:'APPROVAL_EXPIRED'});
const closed={run_id:'current-run',status:'cancelled',pending:proposal};

test('expired close restores conversation controls and calls no creation endpoint',async()=>{
  const h=harness([closed]);
  await vm.runInContext("agentAction('cancel',undefined,'current-run')",h.context);
  assert.deepEqual(h.calls.map(c=>c.path),['/agent/current-run/cancel']);
  assert.equal(h.calls[0].identity,'engineer');
  assert.equal(h.state.agent.status,'cancelled');assert.equal(h.state.busy,false);
  assert.match(h.notices[0].text,/已关闭，未创建工单/);
});
test('refusal expiring in transit retries only through cancel',async()=>{
  const h=harness([expired(),closed]);
  await vm.runInContext("agentAction('approval',{approve:false},'current-run')",h.context);
  assert.deepEqual(h.calls.map(c=>c.path),['/agent/current-run/approval','/agent/current-run/cancel']);
  assert.equal(h.state.agent.status,'cancelled');assert.equal(h.state.busy,false);
});
test('an expired creation request is not cancelled or approved automatically',async()=>{
  const h=harness([expired()]);
  await vm.runInContext("agentAction('approval',{approve:true},'current-run')",h.context);
  assert.deepEqual(h.calls.map(c=>c.path),['/agent/current-run/approval']);
  assert.equal(h.refreshes,1);assert.equal(h.state.agent.status,'waiting_approval');
  assert.match(h.notices[0].text,/请关闭过期预览/);
});
test('failed cancellation retains the pending task and does not claim success',async()=>{
  const h=harness([new Error('连接失败')]);
  await vm.runInContext("agentAction('cancel',undefined,'current-run')",h.context);
  assert.equal(h.state.agent.status,'waiting_approval');assert.equal(h.state.busy,false);
  assert.equal(h.rendered.length,0);assert.equal(h.notices[0].error,true);
});
test('a historical preview cannot cancel the current task',async()=>{
  const h=harness([]);
  await vm.runInContext("agentAction('cancel',undefined,'another-run')",h.context);
  assert.equal(h.calls.length,0);assert.equal(h.state.agent.status,'waiting_approval');
});
