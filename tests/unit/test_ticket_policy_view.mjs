import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const source=await readFile(new URL('../../app/web/agent-view.js',import.meta.url),'utf8');
const {proposalView,receiptView,ticketStatus}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
class Element{
  constructor(tag){this.tag=tag;this.children=[];this.dataset={};this.events={};}
  append(...items){this.children.push(...items);}
  setAttribute(){}
  addEventListener(name,callback){this.events[name]=callback;}
}
globalThis.document={createElement:tag=>new Element(tag)};
const flatten=node=>[node,...node.children.flatMap(flatten)];
const text=node=>flatten(node).map(item=>item.textContent||'').join(' ');
const proposal=category=>({version:1,expires_at:Date.now()/1000+600,arguments:{title:'测试工单标题',description:'测试工单内容'},classification:{category,category_label:'服务端分类',reason:'必须核对实际操作',status_label:'待业务审批'}});

test('sensitive submission is not described as business approval',()=>{
  const view=proposalView(proposal('sensitive'),{approve(){},revise(){}});
  assert.match(text(view),/提交确认不等于业务审批/);
  assert.equal(flatten(view).find(item=>item.textContent==='确认提交').disabled,false);
});
test('prohibited proposals cannot be submitted but remain editable or cancellable',()=>{
  const view=proposalView(proposal('prohibited'),{approve(){},revise(){}});
  assert.equal(flatten(view).find(item=>item.textContent==='确认提交').disabled,true);
  assert.equal(flatten(view).find(item=>item.textContent==='修改内容').disabled,false);
});
test('receipts and status badges distinguish pending approval from execution',()=>{
  assert.equal(ticketStatus({status:'pending_approval'}),'待业务审批');
  assert.equal(ticketStatus({status:'approved'}),'审批通过 · 未执行');
  const view=receiptView({result:{ticket_id:'ticket',classification:{category_label:'敏感申请',status_label:'待业务审批'}}});
  assert.match(text(view),/敏感申请 · 待业务审批/);assert.match(text(view),/不代表业务审批通过或已执行/);
});
