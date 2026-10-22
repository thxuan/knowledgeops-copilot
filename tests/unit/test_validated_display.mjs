import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {runInNewContext} from 'node:vm';

async function module(name){
  const source=await readFile(new URL(`../../app/web/${name}.js`,import.meta.url),'utf8');
  return import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
}
const {validatedAnswerText,animateValidatedText}=await module('validated-display');
const {readEventStream}=await module('event-stream');
const {createStreamControl}=await module('stream-control');
const workbench=await readFile(new URL('../../app/web/workbench.js',import.meta.url),'utf8');
const requestSource=workbench.slice(workbench.indexOf('async function streamRequest('),workbench.indexOf('async function batchRequest('));
const final={status:'answered',answer:'文档明确说明：先登录，再申请权限。',citations:[{chunk_id:'source-1'}]};

test('only final knowledge answers and clarification questions enter the animation',()=>{
  assert.equal(validatedAnswerText(final),final.answer);
  assert.equal(validatedAnswerText({kind:'question',response:final}),final.answer);
  assert.equal(validatedAnswerText({status:'needs_clarification',answer:'',follow_up:'哪个版本？'}),'需要你补充\n哪个版本？');
  for(const result of [null,{kind:'agent',response:final},{status:'rejected',answer:'bad'},{text:'draft'}])assert.equal(validatedAnswerText(result),'');
});

function clock(){
  let time=0;
  return {now:()=>time,wait:async()=>{time+=40;},elapsed:()=>time};
}

test('validated Chinese and emoji appear progressively without splitting graphemes',async()=>{
  const text='中文👨‍👩‍👧‍👦é，正文完成。',frames=[],time=clock();
  await animateValidatedText(text,value=>frames.push(value),{...time,reducedMotion:false});
  assert.equal(frames.at(-1),text);assert.ok(frames.length>2);assert.notEqual(frames[0],text);
  const boundaries=new Set(Array.from(new Intl.Segmenter(undefined,{granularity:'grapheme'}).segment(text),part=>part.index+part.segment.length));
  for(const frame of frames){assert.ok(text.startsWith(frame));assert.ok(boundaries.has(frame.length));}
});

test('long answers finish within the display duration instead of adding a per-character delay',async()=>{
  const text='已校验正文。'.repeat(1500),time=clock(),frames=[];
  await animateValidatedText(text,value=>frames.push(value),{...time,reducedMotion:false});
  assert.equal(frames.at(-1),text);assert.ok(time.elapsed()<=2440);
});

test('reduced motion shows the complete answer immediately',async()=>{
  const frames=[];
  await animateValidatedText(final.answer,value=>frames.push(value),{reducedMotion:true,wait:()=>assert.fail('must not wait')});
  assert.deepEqual(frames,[final.answer]);
});

test('changing conversation stops further display updates',async()=>{
  const frames=[];let current=true;
  await animateValidatedText(final.answer,value=>frames.push(value),{reducedMotion:false,isCurrent:()=>current,wait:async()=>{current=false;}});
  assert.equal(frames.length,1);assert.notEqual(frames[0],final.answer);
  await animateValidatedText(final.answer,()=>assert.fail('stale display'),{isCurrent:()=>false});
});

function harness(){
  let sender,calls=0,time=0,animations=0;
  const elements=[],generation={textContent:''};
  const pending={replaceChildren(...children){this.children=children;},setAttribute(){}};
  const body=new ReadableStream({start(controller){sender=controller;}});
  const context={state:{epoch:1},activeStream:null,AbortController,setTimeout,clearTimeout,
    updateControls(){},createStreamControl,readEventStream,validatedAnswerText,
    el(tag,className,text=''){
      const item={tag,className,writes:[],_text:text};
      Object.defineProperty(item,'textContent',{get(){return this._text;},set(value){this._text=value;this.writes.push(value);}});
      elements.push(item);return item;
    },messageLabel:()=>({}),$:()=>generation,scrollFollower:{follow(){}},
    fetch:async()=>{calls++;return {ok:true,body};},
    animateValidatedText:async(text,render,options)=>{
      animations++;
      assert.equal(context.activeStream.canStop,false,'saved results cannot be cancelled as model generation');
      assert.equal(generation.textContent,'校验已通过，正在显示回答…');
      await animateValidatedText(text,render,{...options,reducedMotion:false,now:()=>time,wait:async()=>{time+=80;}});
    }
  };
  const request=runInNewContext(`(${requestSource})`,context);
  return {context,pending,generation,
    start:()=>request('/workbench/message/stream',{},'user',pending,1),
    emit:(kind,data)=>sender.enqueue(new TextEncoder().encode(`event: ${kind}\ndata: ${JSON.stringify(data)}\n\n`)),
    close:()=>sender.close(),
    preview:()=>elements.find(item=>item.className.includes('streaming-preview')),
    calls:()=>calls,animations:()=>animations
  };
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));

test('actual request waits for final success, hides drafts, then displays once with citations intact',async()=>{
  const h=harness(),promise=h.start();
  h.emit('phase',{stage:'generating'});h.emit('preview',{text:'未经校验的错误正文'});await flush();
  assert.equal(h.preview().textContent,'');assert.equal(h.animations(),0);
  assert.match(h.generation.textContent,/内容待校验/);
  h.emit('phase',{stage:'validating'});await flush();assert.equal(h.preview().textContent,'');
  const response={kind:'question',response:final};h.emit('result',response);await flush();
  assert.equal(h.preview().textContent,'','an incomplete transport cannot start the presentation');
  h.close();const result=await promise;
  assert.deepEqual(result,response);assert.equal(h.calls(),1);assert.equal(h.animations(),1);
  assert.ok(h.preview().writes.length>2);assert.equal(h.preview().textContent,final.answer);
  assert.equal(h.context.activeStream,null);
});

test('a stream error after a draft never displays draft text',async()=>{
  const h=harness(),promise=h.start(),rejection=assert.rejects(promise,/引用失效/);
  h.emit('preview',{text:'错误正文'});h.emit('error',{code:'INVALID_CITATION',message:'引用失效'});h.close();
  await rejection;assert.equal(h.animations(),0);assert.equal(h.preview().textContent,'');assert.equal(h.context.activeStream,null);
});

test('connection loss before a final result cannot turn a draft into an answer',async()=>{
  const h=harness(),promise=h.start(),rejection=assert.rejects(promise,/连接已中断/);
  h.emit('preview',{text:'半截正文'});h.close();await rejection;
  assert.equal(h.preview().textContent,'');assert.equal(h.animations(),0);
});

test('a terminal error after a result still prevents displaying that result',async()=>{
  const h=harness(),promise=h.start(),rejection=assert.rejects(promise,/保存失败/);
  h.emit('result',final);h.emit('error',{message:'保存失败'});h.close();await rejection;
  assert.equal(h.animations(),0);assert.equal(h.preview().textContent,'');
});

test('Agent and refused results keep their existing immediate presentation path',async()=>{
  for(const result of [{kind:'agent',agent:{run_id:'task-1'}},{status:'refused',answer:'没有可访问的依据'}]){
    const h=harness(),promise=h.start();h.emit('result',result);h.close();
    assert.deepEqual(await promise,result);assert.equal(h.animations(),0);
  }
});

test('a late result from an earlier conversation does not update the new conversation',async()=>{
  const h=harness(),promise=h.start();await flush();h.context.state.epoch=2;
  h.emit('result',final);h.close();await promise;
  assert.equal(h.animations(),0);assert.equal(h.preview().textContent,'');
});
