import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const source=await readFile(new URL('../../app/web/scroll-follow.js',import.meta.url),'utf8');
const {createScrollFollower}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
function harness(){
  const events={},jumpEvents={},container={scrollHeight:1000,clientHeight:400,scrollTop:600,addEventListener(type,callback){events[type]=callback;}},jump={hidden:true,addEventListener(type,callback){jumpEvents[type]=callback;}};
  let time=1000;
  return {container,jump,events,jumpEvents,advance:value=>{time+=value;},follower:createScrollFollower(container,jump,()=>time)};
}
test('new output follows only while the reader is pinned to the bottom',()=>{
  const fixture=harness();fixture.container.scrollHeight=1200;fixture.follower.follow();assert.equal(fixture.container.scrollTop,1200);
  fixture.events.wheel({type:'wheel'});fixture.container.scrollTop=300;fixture.events.scroll();fixture.advance(200);fixture.container.scrollHeight=1600;fixture.follower.follow();
  assert.equal(fixture.container.scrollTop,300);assert.equal(fixture.jump.hidden,false);
});
test('jumping to latest restores following and hides the control',()=>{
  const fixture=harness();fixture.container.scrollTop=0;fixture.events.scroll();assert.equal(fixture.jump.hidden,false);
  fixture.jumpEvents.click();assert.equal(fixture.container.scrollTop,1000);assert.equal(fixture.jump.hidden,true);
  fixture.container.scrollHeight=1400;fixture.follower.follow();assert.equal(fixture.container.scrollTop,1400);
});
test('manual scroll cooldown survives returning near the bottom',()=>{
  const fixture=harness();fixture.events.keydown({type:'keydown',key:'PageDown'});fixture.container.scrollTop=580;fixture.events.scroll();fixture.container.scrollHeight=1100;
  fixture.advance(149);fixture.follower.follow();assert.equal(fixture.container.scrollTop,580);
  fixture.advance(2);fixture.follower.follow();assert.equal(fixture.container.scrollTop,1100);
});
test('touch scrolling remains unpinned even after the cooldown expires',()=>{
  const fixture=harness();fixture.events.touchmove({type:'touchmove'});fixture.container.scrollTop=0;fixture.events.scroll();fixture.advance(500);fixture.follower.follow();assert.equal(fixture.container.scrollTop,0);
});
