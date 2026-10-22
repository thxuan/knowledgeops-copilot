// Keep the ticket-page processing tab available even when its queue is empty.
export function installTicketInbox({auth,fetch,doc=document,win=window}){
  const reviewScopes=['triage','access_review','security_review','production_review'];
  if(!auth.ticketCapabilities.some(scope=>reviewScopes.includes(scope)))return null;
  const button=doc.getElementById('review-tickets');if(!button)return null;
  button.hidden=false;
  let serial=0,controller=null,disposed=false;
  function show(count,error=false){
    const value=Number.isInteger(count)&&count>=0?count:null;
    const description=error?'数量暂时无法读取，点击查看处理队列':value===null?'正在读取工单数量':value===0?'暂无待处理工单':`处理队列中有 ${value} 条工单`;
    if(button){button.textContent=`待我处理 · ${value===null?'—':value}`;button.title=description;}
  }
  async function refresh(){
    if(disposed||doc.hidden)return;
    const current=++serial;controller?.abort();controller=new AbortController();
    const requestController=controller,timer=win.setTimeout(()=>requestController.abort(),10000);
    try{
      const options={signal:requestController.signal,headers:{'X-Local-User-Id':auth.user},cache:'no-store'};
      const runtime=await fetch('/workbench/datasets/runtime',options);
      if(!runtime.ok)throw new Error('Workspace unavailable');
      const scope=await runtime.json();
      const response=await fetch('/ticket-reviews?limit=1',{...options,headers:{...options.headers,'X-Knowledge-Workspace':scope.fingerprint}});
      if(!response.ok)throw new Error('Inbox unavailable');
      const result=await response.json();
      if(current===serial&&!disposed)show(result.total);
    }catch(error){if(current===serial&&!disposed)show(null,true);}
    finally{win.clearTimeout(timer);}
  }
  const onVisible=()=>{if(!doc.hidden)refresh();};
  doc.addEventListener('visibilitychange',onVisible);win.addEventListener('focus',onVisible);
  win.addEventListener('knowledgeops:ticket-queue-changed',onVisible);
  const timer=win.setInterval(onVisible,30000);refresh();
  return {refresh,dispose(){disposed=true;++serial;controller?.abort();win.clearInterval(timer);doc.removeEventListener('visibilitychange',onVisible);win.removeEventListener('focus',onVisible);win.removeEventListener('knowledgeops:ticket-queue-changed',onVisible);}};
}
