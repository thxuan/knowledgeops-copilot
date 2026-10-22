import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const source=await readFile(new URL('../../app/web/follow-up.js',import.meta.url),'utf8');
const {appendFollowUp}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
function node(tag){return {tag,children:[],attributes:{},textContent:'',append(...items){this.children.push(...items);},setAttribute(k,v){this.attributes[k]=v;}};}
const doc={createElement:node};
test('follow-up is an independent plain-text block, including strings containing HTML',()=>{
 const body=node('div'),question='<img src=x onerror=alert(1)> 你使用什么系统？';
 const block=appendFollowUp(body,{follow_up:question},doc);
 assert.equal(body.children[0],block);assert.equal(block.attributes['aria-label'],'需要补充的信息');
 assert.equal(block.children[1].textContent,question);assert.equal(block.children[1].children.length,0);
});
test('legacy and sufficient answers render without an empty follow-up block',()=>{
 const body=node('div');for(const response of [{},{follow_up:null},{follow_up:'  '}])assert.equal(appendFollowUp(body,response,doc),null);
 assert.equal(body.children.length,0);
});
