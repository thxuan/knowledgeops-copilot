import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
const source=await readFile(new URL('../../app/web/workbench.js',import.meta.url),'utf8');
const restore=source.slice(source.indexOf('function restoreConversationRuns()'),source.indexOf('async function restoreCache()'));
function render(runs,agent=null,extra={}){
 let welcome=0,cleared=0,agentRenders=0;const messages=[];
 const context=vm.createContext({state:{runs,agent,...extra},$:()=>({replaceChildren(){cleared++;}}),
  showWelcome(){welcome++;},clearCancelledConversation(){welcome++;},renderAgent(){agentRenders++;},message(role,text){messages.push([role,text]);}});
 vm.runInContext(restore,context);vm.runInContext('restoreConversationRuns()',context);
 return {welcome,cleared,agentRenders,messages};
}
test('batch-only history restores the task launcher instead of clearing the conversation',()=>{
 const runs=Array.from({length:12},(_,i)=>({id:'r'+i,batch_id:'batch',view:'evaluation',response:{answer:'answer'}}));
 const r=render(runs);assert.equal(r.welcome,1);assert.equal(r.cleared,0);assert.deepEqual(r.messages,[]);
});
test('only actual chat runs restore into the conversation',()=>{
 const r=render([{batch_id:'b',view:'evaluation'},{view:'evaluation'}, {message:'Chat question',response:{answer:'Chat answer'}}]);
 assert.equal(r.welcome,0);assert.equal(r.cleared,1);
 assert.deepEqual(r.messages,[['user','Chat question'],['assistant','Chat answer']]);
});
test('an active Agent remains visible when there are only evaluation records',()=>{
 const r=render([{batch_id:'b',view:'evaluation'}],{run_id:'active'});
 assert.equal(r.welcome,0);assert.equal(r.cleared,0);assert.equal(r.agentRenders,1);
});
test('an empty cache displays the task launcher',()=>{
 assert.equal(render([]).welcome,1);
});

test('refresh restores only the active session and excludes read-only reruns',()=>{
 const runs=[{id:'old',message:'Old session',response:{conversation_id:'old',answer:'Old answer'}},{id:'current',message:'Current question',response:{conversation_id:'current',answer:'Current answer'}},{id:'replay',message:'Readonly retry',request:{replay_context_id:'checkpoint'},response:{conversation_id:'current',answer:'Retry answer'}}];
 assert.deepEqual(render(runs,null,{sessionId:'current'}).messages,[['user','Current question'],['assistant','Current answer']]);
 assert.equal(render(runs,null,{sessionId:'brand-new'}).welcome,1);
});

test('completed task records restore alongside questions in the active session',()=>{
 const result=render([],null,{sessionId:'current',agentRuns:[{message:'Task request',record_meta:{session_id:'current'},agent:{run_id:'task'}}]});
 assert.equal(result.agentRenders,1);assert.deepEqual(result.messages,[['user','Task request']]);
});

test('a cancelled last task restores a blank new conversation rather than cancelled content',()=>{
 const result=render([{message:'Earlier question',record_meta:{session_id:'current'},response:{answer:'Earlier answer'}}],null,{sessionId:'current',agentRuns:[{message:'Cancelled request',record_meta:{session_id:'current'},agent:{run_id:'task',status:'cancelled'}}]});
 assert.equal(result.welcome,1);assert.equal(result.agentRenders,0);assert.deepEqual(result.messages,[]);
});

test('a cancelled active task cannot reappear after switching identity',()=>{
 const result=render([],{run_id:'cancelled-task',status:'cancelled'});
 assert.equal(result.welcome,1);assert.equal(result.agentRenders,0);assert.deepEqual(result.messages,[]);
});
