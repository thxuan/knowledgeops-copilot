// Only explicitly selected experiment data is stored; connection forms are never read.
export function scopeKey(identity, connection) {
  const d=connection.database,e=connection.embedding;
  return JSON.stringify([identity,d.host,d.port,d.database,d.schema_name,e.collection]);
}
export function recover(snapshot) {
  const copy=structuredClone(snapshot);
  for(const b of copy.batches||[]){
    if(b.status==='running')b.status='interrupted';
    b.completed=(copy.runs||[]).filter(r=>r.batch_id===b.id).length;
    b.errors=(copy.failures||[]).filter(r=>r.batch_id===b.id).length;
    if([...(copy.runs||[]),...(copy.failures||[])].some(r=>r.batch_id===b.id&&r.request?.question_id===b.inFlight))b.inFlight=null;
  }
  return copy;
}
let database;
function open(){return database??=new Promise((resolve,reject)=>{const r=indexedDB.open('knowledgeops-experiments',1);r.onupgradeneeded=()=>r.result.createObjectStore('workspaces');r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error);r.onblocked=()=>reject(new Error('本地记录库被其他标签页阻塞'));});}
export async function read(key){const db=await open();return new Promise((resolve,reject)=>{const tx=db.transaction('workspaces'),r=tx.objectStore('workspaces').get(key);r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error);});}
export async function write(key,value){const db=await open();return new Promise((resolve,reject)=>{const tx=db.transaction('workspaces','readwrite');tx.objectStore('workspaces').put(value,key);tx.oncomplete=()=>resolve();tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error);});}
export async function remove(key){const db=await open();return new Promise((resolve,reject)=>{const tx=db.transaction('workspaces','readwrite');tx.objectStore('workspaces').delete(key);tx.oncomplete=()=>resolve();tx.onerror=()=>reject(tx.error);});}

export function remainingQuestions(questions,batchId,runs,failures){
  const finished=new Set([...runs,...failures].filter(r=>r.batch_id===batchId).map(r=>r.request.question_id));
  return questions.filter(q=>!finished.has(q.question_id));
}
