import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const source=await readFile(new URL('../../app/web/stream-control.js',import.meta.url),'utf8');
const {createStreamControl}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
test('routing cannot be mistaken for a cancellable knowledge answer',()=>{
  const stream=createStreamControl();stream.phase('routing');assert.equal(stream.stop(),false);assert.equal(stream.controller.signal.aborted,false);
});
test('stopping a knowledge answer aborts its request exactly once',()=>{
  const stream=createStreamControl();stream.phase('generating');assert.equal(stream.stop(),true);assert.equal(stream.stopped,true);assert.equal(stream.controller.signal.aborted,true);assert.equal(stream.stop(),false);stream.phase('review');assert.equal(stream.canStop,false);
});
test('saving a result disables stop before persistence completes',()=>{
  const stream=createStreamControl();stream.phase('validating');assert.equal(stream.canStop,true);stream.phase('saving');assert.equal(stream.stop(),false);assert.equal(stream.stopped,false);
});
