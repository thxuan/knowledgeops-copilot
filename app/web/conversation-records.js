export function isEvaluation(run){return !!(run.batch_id||run.request?.question_id||run.dataset||run.view==='evaluation'&&!run.record_meta);}
export function sessionKey(run){return run.record_meta?.session_id||run.response?.conversation_id||run.request?.conversation_id||run.id;}
export function entries(runs=[],failures=[],agents=[]){return [...runs.filter(run=>!isEvaluation(run)),...failures.filter(run=>!isEvaluation(run)),...agents].sort((left,right)=>(left.created||'').localeCompare(right.created||''));}
export function groups(runs,failures,agents){
  const sessions=new Map();
  for(const entry of entries(runs,failures,agents)){
    const key=sessionKey(entry);if(!key)continue;
    if(!sessions.has(key))sessions.set(key,{id:key,title:entry.message||entry.request?.message||'历史自由会话',turns:[],updated:entry.created||''});
    const session=sessions.get(key),turnId=entry.record_meta?.turn_id||entry.id||entry.created;
    let turn=session.turns.find(item=>item.id===turnId);
    if(!turn){turn={id:turnId,number:entry.record_meta?.turn_number||session.turns.length+1,message:entry.message||entry.request?.message||'',attempts:[]};session.turns.push(turn);}
    turn.attempts.push(entry);session.updated=entry.created||session.updated;
  }
  return [...sessions.values()].sort((left,right)=>right.updated.localeCompare(left.updated));
}
export function nextMeta(sessionId,records,baseline=null){
  if(baseline?.record_meta){const meta=baseline.record_meta;return {...meta,attempt:1+Math.max(0,...records.filter(run=>run.record_meta?.turn_id===meta.turn_id).map(run=>run.record_meta.attempt||1))};}
  const previous=records.filter(run=>sessionKey(run)===sessionId),turns=groups(previous,[],[])[0]?.turns||[],last=turns.reduce((latest,turn)=>!latest||turn.number>latest.number?turn:latest,null);
  return {session_id:sessionId,turn_id:crypto.randomUUID(),turn_number:1+(last?.number||0),parent_turn_id:last?.id||null,attempt:1};
}
export function sameHistory(left,right){
  const first=left.response?.context?.history_snapshot?.fingerprint,second=right.response?.context?.history_snapshot?.fingerprint;
  return first&&second?first===second:!left.request?.conversation_id&&!right.request?.conversation_id&&!left.request?.replay_context_id&&!right.request?.replay_context_id;
}
