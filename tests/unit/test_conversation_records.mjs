import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const source=await readFile(new URL('../../app/web/conversation-records.js',import.meta.url),'utf8');
const records=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const run=(id,session,turn,attempt=1)=>({id,message:'问题 '+turn,created:'2026-10-17T10:00:0'+attempt+'Z',record_meta:{session_id:session,turn_id:turn,turn_number:Number(turn.slice(-1)),attempt},response:{conversation_id:session}});

test('sessions contain ordered turns with separate attempts and failures',()=>{
  const first=run('one','session','turn1'),retry=run('retry','session','turn1',2),next=run('next','session','turn2'),other=run('other','other','turn1');
  const failure={...run('failure','session','turn1'),response:undefined,error:'连接中断'};
  const sessions=records.groups([first,retry,next,other],[failure],[]),session=sessions.find(item=>item.id==='session');
  assert.equal(sessions.length,2);assert.equal(session.turns.length,2);assert.equal(session.turns[0].attempts.length,3);
  assert.equal(session.title,'问题 turn1');assert.equal(session.turns[1].number,2);
});

test('retries retain the same turn while a followup creates a new turn',()=>{
  const first=run('one','session','turn1'),retry=run('retry','session','turn1',2);
  const meta=records.nextMeta('session',[first,retry],first);
  assert.equal(meta.turn_id,'turn1');assert.equal(meta.attempt,3);assert.equal(meta.turn_number,1);
  const followup=records.nextMeta('session',[first,retry]);assert.notEqual(followup.turn_id,'turn1');assert.equal(followup.turn_number,2);assert.equal(followup.parent_turn_id,'turn1');
});

test('dataset runs are excluded and legacy conversation IDs are grouped without invented retries',()=>{
  const legacy=[{id:'a',message:'首问',response:{conversation_id:'legacy'}},{id:'b',message:'追问',request:{conversation_id:'legacy'},response:{conversation_id:'legacy'}},{id:'dataset',request:{question_id:'Q1'}}];
  const sessions=records.groups(legacy,[],[]);assert.equal(sessions.length,1);assert.equal(sessions[0].turns.length,2);assert.equal(sessions[0].turns[1].number,2);
});

test('task polls remain one request rather than additional turns',()=>{
  const task={...run('task','session','turn1'),response:undefined,agent:{status:'completed',trace:[{},{}]}};
  const sessions=records.groups([],[],[task]);assert.equal(sessions[0].turns.length,1);assert.equal(sessions[0].turns[0].attempts.length,1);
});

test('legacy followups keep their turn numbering and late reruns do not change the parent turn',()=>{
  const old=[{id:'old1',response:{conversation_id:'legacy'}},{id:'old2',response:{conversation_id:'legacy'}}];
  const next=records.nextMeta('legacy',old);assert.equal(next.turn_number,3);assert.equal(next.parent_turn_id,'old2');
  const first=run('first','session','turn1'),second=run('second','session','turn2'),late=run('late','session','turn1',2);
  const followup=records.nextMeta('session',[first,second,late]);assert.equal(followup.turn_number,3);assert.equal(followup.parent_turn_id,'turn2');
});

test('history comparisons require the same fixed fingerprint for multi-turn requests',()=>{
  const make=fingerprint=>({request:{conversation_id:'live'},response:{context:{history_snapshot:{fingerprint}}}});
  assert.equal(records.sameHistory(make('same'),make('same')),true);assert.equal(records.sameHistory(make('first'),make('later')),false);
  assert.equal(records.sameHistory({request:{conversation_id:'live'}},{request:{conversation_id:'live'}}),false);
  assert.equal(records.sameHistory({request:{}},{request:{}}),true);
});
