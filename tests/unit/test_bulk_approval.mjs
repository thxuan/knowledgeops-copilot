import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const load=async path=>import('data:text/javascript;base64,'+Buffer.from(await readFile(new URL(path,import.meta.url),'utf8')).toString('base64'));
const {approvalItems,approveReviewItem}=await load('../../app/web/bulk-approval.js');
const {DocumentQueue}=await load('../../app/web/knowledge-queue.js');
const review=(id,readers=['owner','employee'])=>({review_id:id,document_id:'doc-'+id,version_id:'v-'+id,label:'v1',title:id,user_ids:readers,submitted_by:'owner',status:'pending',withdrawn:false});

test('only administrators may prepare a nonempty batch of up to 100 pending reviews',()=>{
 assert.throws(()=>approvalItems([review('one')],['one'],false),/平台管理员/);
 assert.throws(()=>approvalItems([],[],true),/1–100/);
 assert.throws(()=>approvalItems([],Array.from({length:101},(_,i)=>String(i)),true),/1–100/);
 for(const row of [{...review('one'),status:'approved'},{...review('one'),withdrawn:true}])assert.throws(()=>approvalItems([row],['one'],true),/已变化/);
 assert.throws(()=>approvalItems([],['missing'],true),/已变化/);
 assert.equal(approvalItems([review('one')],['one','one'],true).length,1);
});
test('confirmation freezes each requested version and readership; approval sends only the comment to the exact review ID',async()=>{
 const source=[review('rd',['owner','engineer']),review('it',['owner','support'])];
 const items=approvalItems(source,['rd','it'],true);source[0].user_ids.push('unexpected');
 assert.deepEqual(items.map(x=>x.reader_user_ids),[['owner','engineer'],['owner','support']]);
 const requests=[];
 for(const item of items)await approveReviewItem(item,'已核对',async(path,body)=>{requests.push({path,body});return {status:'approved'};});
 assert.deepEqual(requests,[{path:'/document-reviews/rd/approve',body:{comment:'已核对'}},{path:'/document-reviews/it/approve',body:{comment:'已核对'}}]);
});
test('a stale review fails independently and retry does not repeat successful approval',async()=>{
 const items=approvalItems([review('good'),review('stale')],['stale','good'],true),calls=[];let stale=true;
 const q=new DocumentQueue(items,item=>approveReviewItem(item,'',async path=>{calls.push(path);if(path.includes('/stale/')&&stale)throw new Error('review invalidated');return {status:'approved'};}));
 await q.run();assert.deepEqual(items.map(x=>x.status),['failed','succeeded']);
 stale=false;await q.run('failed');assert.deepEqual(items.map(x=>x.status),['succeeded','succeeded']);
 assert.deepEqual(calls,['/document-reviews/stale/approve','/document-reviews/good/approve','/document-reviews/stale/approve']);
});
test('lost response can safely reuse an already approved request',async()=>{
 const item=approvalItems([review('one')],['one'],true)[0];let posted=0;
 const q=new DocumentQueue([item],row=>approveReviewItem(row,'',async()=>{posted++;if(posted===1)throw new Error('lost response');return {status:'approved',reused:true};}));
 await q.run();assert.equal(item.status,'failed');await q.run('failed');
 assert.equal(item.status,'skipped');assert.equal(item.result.reused,true);assert.match(item.detail,/复用/);
});
test('workspace changes stop later approvals and never replace an invalid request with another review ID',async()=>{
 const items=approvalItems([review('old'),review('second')],['old','second'],true),calls=[];
 const q=new DocumentQueue(items,item=>approveReviewItem(item,'',async path=>{calls.push(path);throw Object.assign(new Error('workspace changed'),{code:'WORKSPACE_CHANGED'});}));
 await q.run();assert.deepEqual(calls,['/document-reviews/old/approve']);assert.deepEqual(items.map(x=>x.status),['failed','pending']);
});
