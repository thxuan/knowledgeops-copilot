import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const load=async path=>import('data:text/javascript;base64,'+Buffer.from(await readFile(new URL(path,import.meta.url),'utf8')).toString('base64'));
const {prepareReviewBatch,submitReviewItem}=await load('../../app/web/bulk-review.js');
const {DocumentQueue}=await load('../../app/web/knowledge-queue.js');
const doc=id=>({document_id:id,title:id,uploaded_by:'owner',can_manage:true,status:'succeeded'});
const version=(id='v1',extra={})=>({version_id:id,label:id,build_status:'ready',build_updated_at:123,base_revision:1,current:true,was_published:false,origin:'upload',...extra});
const state=(versions=[version()],extra={})=>({revision:1,withdrawn:false,publication_state:'draft',versions,...extra});
const prepared=async(s=state())=>(await prepareReviewBatch([doc('one')],async()=>s))[0];

test('preflight preserves pending requests and skips unready, withdrawn, stale and published versions',async()=>{
 const states={ready:state(),unready:state([version('v1',{build_status:'queued'})]),pending:state([version('v1',{review:{status:'pending'}})]),withdrawn:state(undefined,{withdrawn:true}),stale:state([version('v1',{base_revision:0})]),published:state([version('v1',{was_published:true})],{publication_state:'published'})};
 const calls=[];
 const items=await prepareReviewBatch(Object.keys(states).map(doc),async path=>{calls.push(path);return states[path.split('/')[2]];});
 assert.deepEqual(items.map(x=>x.status),['pending','skipped','skipped','skipped','skipped','skipped']);
 assert.equal(items[0].version_id,'v1');assert.match(items[2].detail,/已有待审批/);assert.equal(calls.length,6);
});
test('multiple ready drafts require an explicit version choice',async()=>{
 const item=await prepared(state([version('v1',{current:false}),version('v2',{current:false})]));
 assert.equal(item.version_id,undefined);
 await assert.rejects(()=>submitReviewItem(item,['reader'],async()=>{throw new Error('must not send');}),/请选择/);
});
test('submitting includes each owner and binds the chosen version, revision and exact readers',async()=>{
 const item=await prepared();const calls=[];
 const result=await submitReviewItem(item,['reader','reader'],async(path,body)=>{calls.push({path,body});return body?{review_id:'review-1'}:state();});
 assert.equal(calls.length,2);assert.equal(calls[1].path,'/documents/one/versions/v1/review');
 assert.deepEqual(calls[1].body,{expected_revision:1,user_ids:['owner','reader']});assert.equal(result.review_id,'review-1');
});
test('changed revision, rebuilt artifact and newly pending different scope prevent submission',async()=>{
 for(const changed of [state(undefined,{revision:2}),state([version('v1',{build_updated_at:456})]),state([version('v1',{review:{status:'pending',user_ids:['owner','other']}})]),state(undefined,{withdrawn:true})]){
   const item=await prepared();let posts=0;
   await assert.rejects(()=>submitReviewItem(item,['reader'],async(path,body)=>{if(body)posts++;return changed;}));
   assert.equal(posts,0);
 }
});
test('retry after a lost response keeps the identical pending review and does not retry successful documents',async()=>{
 let pending=false,lost=true;const posts=[];
 const api=async(path,body)=>{
   if(!body)return state([version('v1',pending?{review:{status:'pending',user_ids:['reader','owner']}}:{})]);
   posts.push(path);pending=true;
   if(lost){lost=false;throw new Error('lost response');}
   return {review_id:'same-review'};
 };
 const item=await prepared();const skipped={id:'skip',status:'skipped'};
 const q=new DocumentQueue([item,skipped],row=>submitReviewItem(row,['reader'],api));
 await q.run();assert.equal(item.status,'failed');await q.run('failed');
 assert.equal(item.status,'succeeded');assert.equal(item.result.review_id,'same-review');
 await q.run('failed');assert.equal(posts.length,2);assert.equal(skipped.status,'skipped');
});
test('one document failing does not prevent another submission',async()=>{
 const items=await prepareReviewBatch([doc('bad'),doc('good')],async()=>state());
 const q=new DocumentQueue(items,item=>submitReviewItem(item,['reader'],async(path,body)=>{
   if(path.includes('/bad/'))throw new Error('permission changed');
   return body?{review_id:'good-review'}:state();
 }));
 await q.run();assert.deepEqual(items.map(x=>x.status),['failed','succeeded']);
});
test('workspace change during preflight stops the batch rather than collecting mismatched metadata',async()=>{
 let calls=0;
 await assert.rejects(()=>prepareReviewBatch([doc('one'),doc('two')],async()=>{calls++;throw Object.assign(new Error('changed'),{code:'WORKSPACE_CHANGED'});}),/changed/);
 assert.equal(calls,1);
});
test('empty or oversized selections are rejected before any requests',async()=>{
 for(const docs of [[],Array.from({length:101},(_,i)=>doc(String(i)))]){
   await assert.rejects(()=>prepareReviewBatch(docs,async()=>{throw new Error('must not request');}),/1–100/);
 }
});
