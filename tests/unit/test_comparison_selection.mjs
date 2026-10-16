import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
const source=await readFile(new URL('../../app/web/workbench.js',import.meta.url),'utf8');
const code=source.slice(source.indexOf('function comparisonCandidates('),source.indexOf('function comparisonLabel('));
const c=vm.createContext({});vm.runInContext(code,c);
const runs=[{id:'one',message:'Q1'},{id:'two',message:'Q2'},{id:'three',message:'Q1'}];
function candidates(id,filter){c.rows=runs;c.id=id;c.same=filter;return Array.from(vm.runInContext('comparisonCandidates(rows,id,same).map(r=>r.id)',c));}
test('comparison excludes the baseline and defaults to other runs of the same question',()=>{
 assert.deepEqual(candidates('one',true),['three']);
 assert.deepEqual(candidates('three',true),['one']);
});
test('a single run for a question has no fabricated comparison candidate',()=>{
 assert.deepEqual(candidates('two',true),[]);
});
test('explicitly disabling the same-question filter still excludes the baseline',()=>{
 assert.deepEqual(candidates('one',false),['two','three']);
});
test('record table no longer exposes ambiguous row A/B selectors',()=>{
 const table=source.slice(source.indexOf('function renderExperiments()'),source.indexOf('function renderBatchSummary()'));
 assert(!table.includes("['a','b']"));
 assert(table.includes('reviewCell'));
});

test('selecting an older record targets that question and dataset when rerunning',()=>{
 const state={runs:[{id:'old',message:'First question',request:{question_id:'Q1'},dataset:{id:'set1'}}],last:{message:'Last question'}};
 const nodes=new Map();const ctx=vm.createContext({state,$:id=>{if(!nodes.has(id))nodes.set(id,{});return nodes.get(id);},renderMetrics(){},renderEvidence(){},renderExperiments(){},scheduleSave(){}});
 const fn=source.slice(source.indexOf('function selectRun('),source.indexOf('function crossMetric('));
 vm.runInContext(fn,ctx);vm.runInContext("selectRun('old')",ctx);
 assert.equal(state.last.message,'First question');assert.equal(state.last.questionId,'Q1');assert.equal(state.last.dataset.id,'set1');
});
