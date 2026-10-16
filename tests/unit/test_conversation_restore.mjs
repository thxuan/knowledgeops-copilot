import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
const source=await readFile(new URL('../../app/web/workbench.js',import.meta.url),'utf8');
const restore=source.slice(source.indexOf('function restoreConversationRuns()'),source.indexOf('async function restoreCache()'));
function render(runs,agent=null){
 let welcome=0,cleared=0,agentRenders=0;const messages=[];
 const context=vm.createContext({state:{runs,agent},$:()=>({replaceChildren(){cleared++;}}),
  showWelcome(){welcome++;},renderAgent(){agentRenders++;},message(role,text){messages.push([role,text]);}});
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
