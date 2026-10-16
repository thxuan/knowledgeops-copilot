import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const source=await readFile(new URL('../../app/web/agent-view.js',import.meta.url),'utf8');
const {agentFailureMessage,progressView}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
class Element{
  constructor(tag){this.tag=tag;this.children=[];this.dataset={};}
  append(...items){this.children.push(...items);}
  setAttribute(){}
  get childElementCount(){return this.children.length;}
}
globalThis.document={createElement:tag=>new Element(tag)};
function text(n){return [n.textContent||'',...n.children.map(text)].join(' ');}

test('missing Embedding price provides a specific recovery action',()=>{
  const message=agentFailureMessage('UNPRICED_TOOL');
  assert.match(message,/Embedding 单价/);
  assert.match(message,/重新发起任务/);
  assert.doesNotMatch(message,/任务执行失败：UNPRICED_TOOL/);
});
test('a budget-blocked batch marks both outstanding tools as not executed',()=>{
  const agent={status:'failed',outcome:'UNPRICED_TOOL',trace:[],events:[
    {type:'tool_start',name:'query_business_data',call_id:'business'},
    {type:'tool_start',name:'search_knowledge_base',call_id:'search'}]};
  const result=text(progressView(agent));
  assert.equal(result.match(/本轮未执行/g).length,2);
  assert.doesNotMatch(result,/未取得完成结果|进行中/);
});
test('completed traces remain visible when a later batch is blocked',()=>{
  const result=text(progressView({status:'failed',outcome:'UNPRICED_TOOL',
    trace:[{call_id:'old',name:'calculator',status:'succeeded',arguments:{left:1,right:2,operator:'+'},result:{value:3}}],
    events:[{type:'tool_start',call_id:'old',name:'calculator'},{type:'tool_start',call_id:'new',name:'search_knowledge_base'}]}));
  assert.match(result,/1 \+ 2 = 3/);
  assert.equal(result.match(/本轮未执行/g).length,1);
});

test('expired proposals do not keep telling the user to confirm',()=>{
  const result=text(progressView({status:'waiting_approval',pending:{expires_at:0},trace:[],events:[]}));
  assert.match(result,/预览已过期/);
  assert.doesNotMatch(result,/等待你确认/);
});
