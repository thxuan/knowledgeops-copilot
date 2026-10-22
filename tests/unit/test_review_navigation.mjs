import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
const {appendFollowUp}=await import('data:text/javascript;base64,'+Buffer.from(await readFile(new URL('../../app/web/follow-up.js',import.meta.url),'utf8')).toString('base64'));

const source=await readFile(new URL('../../app/web/workbench.js',import.meta.url),'utf8');
const reviewSource=source.slice(source.indexOf('function reviewComplete('),source.indexOf('function exportRuns('));
function environment(){
 const nodes=new Map();
 function node(tag='',cls='',text=''){
  return {tag,className:cls,textContent:text,children:[],value:'',open:false,disabled:false,scrollTop:99,
   append(...items){this.children.push(...items);},replaceChildren(...items){this.children=items;},
   showModal(){this.open=true;},close(){this.open=false;},reportValidity(){return true;}};
 }
 const $=id=>{if(!nodes.has(id))nodes.set(id,node());return nodes.get(id);};
 const run=(id,batch,question)=>({id,batch_id:batch,number:id,request:{question_id:question},message:'Question '+question,
  response:{answer:'Actual answer '+question,evaluation:{reference_points:['Expected '+question]},citations:[],retrieval_trace:{stages:{evidence:[]}}},review:null});
 const state={runs:[run('r1','one','Q1'),run('other','two','OTHER'),run('r2','one','Q2')],current:null,review:null};
 let saves=0;
 const ctx=vm.createContext({appendFollowUp:(container,response)=>appendFollowUp(container,response,{createElement:node}),$,state,el:node,button:(text)=>node('button','',text),
  Option:function(label,value){this.textContent=label;this.value=value;},
  renderExperiments(){},renderMetrics(){},showNotice(){},openSource(){},
  saveCache:async()=>{saves++;return true;}});
 vm.runInContext(reviewSource,ctx);
 return {$,state,ctx,saves:()=>saves};
}
function call(env,code){return vm.runInContext(code,env.ctx);}
test('review navigation selects every record in the same batch and keeps per-run drafts',()=>{
 const e=environment();call(e,"openReview('r1')");
 assert.equal(e.$('review-question').children.length,2);
 assert.equal(e.$('review-previous').disabled,true);
 assert.equal(e.$('review-next').disabled,false);
 e.$('review-notes').value='Draft only for Q1';
 call(e,'moveReview(1)');
 assert.equal(e.state.review,'r2');
 assert.equal(e.$('review-next').disabled,true);
 assert.equal(e.$('review-save-next').textContent,'保存并结束');
 const text=JSON.stringify(e.$('review-reference').children);
 assert(text.includes('Actual answer Q2'));assert(!text.includes('Actual answer Q1'));
 assert.equal(e.$('review-notes').value,'');
 e.$('review-notes').value='Draft only for Q2';
 call(e,'moveReview(-1)');
 assert.equal(e.$('review-notes').value,'Draft only for Q1');
 assert.equal(e.state.runs[0].review,null);
});
test('save-and-next binds the label to the reviewed run and persists before moving',async()=>{
 const e=environment();call(e,"openReview('r1')");
 e.$('correctness').value='pass';e.$('citation-support').value='fail';
 e.$('reviewer').value='Reviewer';e.$('review-notes').value='Citation misses condition';
 await call(e,"saveReview({preventDefault(){},submitter:{id:'review-save-next'}})");
 assert.equal(e.saves(),1);
 assert.equal(e.state.runs[0].review.experiment_id,'r1');
 assert.equal(e.state.runs[0].review.citation_support,'fail');
 assert.equal(e.state.runs[2].review,null);
 assert.equal(e.state.review,'r2');
 assert.equal(e.$('review-dialog').open,true);
 call(e,"openReview('r1')");
 assert.equal(e.$('citation-support').value,'fail');
 assert.equal(e.$('review-notes').value,'Citation misses condition');
});
test('failed local persistence does not advance or discard the reviewed answer',async()=>{
 const e=environment();call(e,"saveCache=async()=>false;openReview('r1')");
 e.$('reviewer').value='Reviewer';e.$('review-notes').value='Notes';
 await call(e,"saveReview({preventDefault(){},submitter:{id:'review-save-next'}})");
 assert.equal(e.state.review,'r1');assert.equal(e.$('review-dialog').open,true);
 assert(e.$('review-save-status').textContent.includes('保存失败'));
 assert.equal(e.$('review-save').disabled,false);
});
test('the final review saves and closes without jumping into another batch',async()=>{
 const e=environment();call(e,"openReview('r2')");
 e.$('reviewer').value='Reviewer';e.$('review-notes').value='Complete';
 e.$('correctness').value='pass';e.$('citation-support').value='pass';
 await call(e,"saveReview({preventDefault(){},submitter:{id:'review-save-next'}})");
 assert.equal(e.$('review-dialog').open,false);assert.equal(e.state.review,null);
 assert.equal(e.state.runs[2].review.experiment_id,'r2');assert.equal(e.state.runs[1].review,null);
});
