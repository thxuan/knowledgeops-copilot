import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
const {appendFollowUp}=await import('data:text/javascript;base64,'+Buffer.from(await readFile(new URL('../../app/web/follow-up.js',import.meta.url),'utf8')).toString('base64'));
const source=await readFile(new URL('../../app/web/workbench.js',import.meta.url),'utf8');
const messageCode=source.slice(source.indexOf('function messageLabel('),source.indexOf('async function runChat('));
class Element {
  constructor(tag='div'){this.tag=tag;this.children=[];this.attributes={};this.events={};this.style={};this.value='';this.scrollHeight=34;}
  append(...children){this.children.push(...children);}
  setAttribute(name,value){this.attributes[name]=value;}
  addEventListener(name,action){this.events[name]=action;}
}
function harness(){
  const log=new Element(),input=new Element(),opened=[];
  const context=vm.createContext({
    appendFollowUp:(container,response)=>appendFollowUp(container,response,{createElement:tag=>new Element(tag)}),
    document:{createElement:tag=>new Element(tag)},
    $:id=>id==='message'?input:log,
    el:(tag,cls='',text='')=>Object.assign(new Element(tag),{className:cls,textContent:String(text)}),
    button:(text,action,cls='')=>{const b=Object.assign(new Element('button'),{textContent:text,className:cls});b.events.click=action;return b;},
    openSource:ref=>opened.push(ref),selectRun(){},showInspector(){},openReview(){},scrollFollower:{follow(){}},
    statusNames:{answered:'已回答'},ms:()=>'',Math
  });
  vm.runInContext(messageCode,context);
  vm.runInContext(source.split('\n').find(line=>line.startsWith('function resizeComposer(')),context);
  return {context,log,input,opened};
}
function all(node){return [node,...node.children.flatMap(all)];}
test('answer sources follow the answer and group cited documents while opening an actual cited chunk',()=>{
  const h=harness(),refs=[
    {document_id:'one',version:'v1',chunk_id:'a',title:'设备合规指南'},
    {document_id:'one',version:'v1',chunk_id:'b',title:'设备合规指南'},
    {document_id:'two',version:'v1',chunk_id:'c',title:'支持流程'}
  ];
  h.context.run={id:'run',response:{status:'answered',answer:'回答',citations:refs,
    claims:[{text:'处理建议',citations:['a','b','c']}],context:{evidence_count:3,timings_ms:{total:1}}}};
  vm.runInContext("message('assistant','回答',run)",h.context);
  const layout=all(h.log).find(node=>node.className==='answer-layout');
  assert.deepEqual(layout.children.map(node=>node.className),['message-body','answer-sources']);
  const links=all(h.log).filter(n=>n.className==='source-link');
  assert.equal(links.length,2);
  links[0].events.click();links[1].events.click();
  assert.deepEqual(h.opened,[refs[0],refs[2]]);
  assert.equal(all(h.log).filter(n=>n.className==='citation-button').length,3);
});

test('answer sources and task records stay below the response at every breakpoint',async()=>{
  const styles=await Promise.all(['conversation.css','ui-theme.css'].map(name=>readFile(new URL('../../app/web/'+name,import.meta.url),'utf8')));
  const layoutRules=styles.join('\n').match(/[^{}]*\.(?:answer-layout|agent-layout)[^{}]*\{[^{}]*\}/g);
  assert.ok(layoutRules.length);
  for(const rule of layoutRules){
    const columns=rule.match(/grid-template-columns:([^;}]+)/);
    if(columns)assert.match(columns[1],/^(?:minmax\(0,1fr\)|1fr)$/);
  }
});
test('answers without citations have no fabricated sources panel',()=>{
  const h=harness();
  vm.runInContext("message('assistant','当前信息不足')",h.context);
  assert.equal(all(h.log).filter(n=>n.className==='answer-sources').length,0);
});
test('long drafts stop growing and clearing the draft restores a compact composer',()=>{
  const h=harness();
  h.input.scrollHeight=600;
  vm.runInContext('resizeComposer()',h.context);
  assert.equal(h.input.style.height,'148px');assert.equal(h.input.style.overflowY,'auto');
  h.input.scrollHeight=34;
  vm.runInContext('resizeComposer()',h.context);
  assert.equal(h.input.style.height,'34px');assert.equal(h.input.style.overflowY,'hidden');
});

test('Enter confirms an IME candidate without sending a message',()=>{
  const fixture=harness();let submitted=0;
  fixture.log.requestSubmit=()=>{submitted++;};
  const handler=source.split('\n').find(line=>line.startsWith("$('message').addEventListener('keydown'"));
  vm.runInContext(handler,fixture.context);
  for(const event of [{key:'Enter',isComposing:true},{key:'Enter',keyCode:229},{key:'Enter',shiftKey:true}])fixture.input.events.keydown({...event,preventDefault(){throw new Error('Composition or newline was intercepted');}});
  assert.equal(submitted,0);
  fixture.input.events.keydown({key:'Enter',preventDefault(){}});assert.equal(submitted,1);
});
