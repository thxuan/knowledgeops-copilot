import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const source=await readFile(new URL('../../app/web/event-stream.js',import.meta.url),'utf8');
const {readEventStream}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
function response(text,fragment=1){
 const bytes=new TextEncoder().encode(text);let i=0;
 return {body:new ReadableStream({pull(c){if(i>=bytes.length)c.close();else{c.enqueue(bytes.slice(i,i+fragment));i+=fragment;}}})};
}
test('SSE handles UTF-8 and event framing split at every byte',async()=>{
 const events=[];
 const result=await readEventStream(response(': heartbeat\r\nevent: preview\r\ndata: {"text":"中文😀"}\r\n\r\nevent: result\r\ndata: {"answer":"最终"}\r\n\r\n'),(kind,data)=>events.push([kind,data]));
 assert.equal(events[0][1].text,'中文😀');assert.equal(result.answer,'最终');assert.equal(events.length,2);
});
test('a partial preview followed by EOF is not a completed answer',async()=>{
 await assert.rejects(readEventStream(response('event: preview\ndata: {"text":"draft"}\n\n'),()=>{}),/连接已中断/);
});
test('structured streaming failures surface the error code and never finalize',async()=>{
 await assert.rejects(readEventStream(response('event: error\ndata: {"code":"REVOKED","message":"权限已撤销"}\n\n'),()=>{}),e=>e.code==='REVOKED');
});
test('a final result without a trailing empty line still completes',async()=>{
 const result=await readEventStream(response('event: result\ndata: {"answer":"complete"}'),()=>{});
 assert.equal(result.answer,'complete');
});
