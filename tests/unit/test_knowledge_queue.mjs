import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const source=await readFile(new URL('../../app/web/knowledge-queue.js',import.meta.url),'utf8');
const {DocumentQueue}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const items=()=>[1,2,3].map(id=>({id,status:'pending'}));
test('failure is isolated; retry never repeats completed or skipped work',async()=>{
 const rows=items(),calls=[];let fail=true;
 const q=new DocumentQueue(rows,async(item,progress)=>{calls.push(item.id);progress('working');if(item.id===2&&fail)throw new Error('temporary');return {reused:item.id===3};});
 await q.run();assert.deepEqual(rows.map(x=>x.status),['succeeded','failed','skipped']);
 fail=false;await q.run('failed');assert.deepEqual(calls,[1,2,3,2]);assert.equal(rows[1].status,'succeeded');
});
test('stop finishes current publication and continuation handles only pending items',async()=>{
 const rows=items(),calls=[];let release;
 const q=new DocumentQueue(rows,async item=>{calls.push(item.id);if(item.id===1)await new Promise(r=>release=r);return {};});
 const running=q.run();await q.run();q.stop();release();await running;
 assert.deepEqual(rows.map(x=>x.status),['succeeded','pending','pending']);
 await q.run();assert.deepEqual(calls,[1,2,3]);assert.equal(q.running,false);
});
test('workspace changes stop remaining requests',async()=>{
 const rows=items();let calls=0;
 const q=new DocumentQueue(rows,async()=>{calls++;throw Object.assign(new Error('changed'),{code:'WORKSPACE_CHANGED'});});
 await q.run();assert.equal(calls,1);assert.deepEqual(rows.map(x=>x.status),['failed','pending','pending']);assert.equal(q.running,false);
});
