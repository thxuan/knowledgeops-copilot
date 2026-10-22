import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const source=await readFile(new URL('../../app/web/index-job-history.js',import.meta.url),'utf8');
const {CompletedJobHistory}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const storage=()=>{const data=new Map();return {getItem:k=>data.get(k)||null,setItem:(k,v)=>data.set(k,v)};};
const jobs=['succeeded','running','queued','failed','blocked','cancelled'].map(status=>({job_id:status,status}));
test('manual refresh dismisses only successful work; polling and page reload do not bring it back',()=>{
 const db=storage(),history=new CompletedJobHistory(db,'employee');
 assert.equal(history.visible(jobs).length,6);assert.equal(history.clearCompleted(jobs),1);
 assert.deepEqual(history.visible(jobs).map(x=>x.status),['running','queued','failed','blocked','cancelled']);
 assert.equal(new CompletedJobHistory(db,'employee').visible(jobs).length,5);
 const later=[...jobs,{job_id:'new-success',status:'succeeded'}];
 assert.equal(history.visible(later).length,6);assert.equal(history.clearCompleted(later),1);
 assert.equal(history.visible(later).length,5);
 assert.equal(history.visible([{job_id:'succeeded',status:'running'}]).length,1);
});
test('dismissal is isolated by current user and can be restored without changing another user',()=>{
 const db=storage(),employee=new CompletedJobHistory(db,'employee'),admin=new CompletedJobHistory(db,'admin');
 employee.clearCompleted(jobs);assert.equal(admin.visible(jobs).length,6);
 admin.clearCompleted(jobs);employee.restore();
 assert.equal(employee.visible(jobs).length,6);assert.equal(admin.visible(jobs).length,5);
 assert.equal(new CompletedJobHistory(db,'employee').visible(jobs).length,6);
});
test('unavailable or malformed browser storage never prevents tasks from being displayed',()=>{
 for(const db of [null,{getItem:()=>'{invalid',setItem:()=>{throw new Error('blocked');}},{getItem:()=>'{"other":1}',setItem:()=>{}}]){
   const history=new CompletedJobHistory(db,'employee');assert.equal(history.visible(jobs).length,6);
   history.clearCompleted(jobs);assert.equal(history.visible(jobs).length,5);history.restore();assert.equal(history.visible(jobs).length,6);
 }
});
