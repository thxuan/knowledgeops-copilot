// Server-owned history; sessionStorage holds only UI selection, drafts and collapse state.
export function installConversationHistory({api,current,isBusy,onOpen,onLock,getDraft,setDraft,onTitle,notify}){
  const $=id=>document.getElementById(id);
  const node=(tag,text='',className='')=>{const n=document.createElement(tag);n.textContent=text;n.className=className;return n;};
  let items=[],cursor=null,scope=null,ui={current:null,drafts:{},expanded:!matchMedia('(max-width:800px)').matches},archived=false,loading=false,request=0,editing=null,before=null;
  const idle=()=>!loading&&!isBusy();
  function persist(){if(scope)try{sessionStorage.setItem(scope,JSON.stringify(ui));}catch{}}
  function rememberDraft(){ui.drafts[current()||'new']=getDraft().slice(0,4000);persist();}
  function expand(value){ui.expanded=value;$('chat-view').classList.toggle('history-open',value);$('toggle-history').setAttribute('aria-expanded',String(value));persist();}
  function sync(){
    for(const b of $('conversation-history').querySelectorAll('button'))b.disabled=!idle();
    $('history-more').hidden=!cursor;$('history-edit-save').disabled=!idle();$('history-archive').disabled=!idle();
    $('history-new').disabled=!idle()||$('new-conversation').disabled;
    if($('history-earlier'))$('history-earlier').disabled=!idle();
    $('history-active').setAttribute('aria-pressed',String(!archived));$('history-archived').setAttribute('aria-pressed',String(archived));
    for(const b of $('history-items').querySelectorAll('[data-conversation-id]'))b.setAttribute('aria-current',String(b.dataset.conversationId===current()));
  }
  function render(){
    const list=$('history-items');list.replaceChildren();
    if(!items.length)list.append(node('p',archived?'暂无已归档会话。':'暂无历史会话，发送消息后自动保存。','field-help'));
    for(const item of items){
      const row=node('div','','history-entry'),open=node('button','','history-entry-open'),title=node('strong',item.title),time=node('small',new Date(item.updated_at*1000).toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}));
      open.type='button';open.dataset.conversationId=item.conversation_id;open.title=item.title;open.append(title,time);open.onclick=()=>openConversation(item.conversation_id);
      const menu=node('button','⋯','history-entry-menu');menu.type='button';menu.setAttribute('aria-label','管理会话：'+item.title);menu.onclick=()=>{
        if(!idle())return;editing=item;$('history-edit-title').value=item.title;$('history-edit-error').textContent='';$('history-archive').textContent=item.archived?'取消归档':'归档会话';$('history-edit-dialog').showModal();$('history-edit-title').focus();
      };
      row.append(open,menu);list.append(row);
    }
    sync();
  }
  async function refresh(more=false){
    const ticket=++request;
    try{
      const params=new URLSearchParams({limit:'20',archived:String(archived)});if(more&&cursor)params.set('cursor',cursor);
      const data=await api('/conversations?'+params);
      if(ticket!==request)return null;
      const key='knowledgeops-chat-ui:'+data.workspace_id+':'+data.user_id;
      if(scope!==key){
        scope=key;ui={current:null,drafts:{},expanded:!matchMedia('(max-width:800px)').matches};
        try{const saved=JSON.parse(sessionStorage.getItem(scope)||'null');if(saved&&typeof saved==='object'){ui.current=typeof saved.current==='string'?saved.current:null;ui.drafts=saved.drafts&&typeof saved.drafts==='object'?saved.drafts:{};if(typeof saved.expanded==='boolean')ui.expanded=saved.expanded;}}catch{}
        expand(ui.expanded);
      }
      const merged=more?[...items,...data.items]:data.items;
      items=[...new Map(merged.map(row=>[row.conversation_id,row])).values()];cursor=data.next_cursor;const active=items.find(item=>item.conversation_id===current());if(active)onTitle(active.title);render();$('history-status').textContent='仅显示本人会话 · 已保存到服务端';return data;
    }catch(error){if(ticket===request){$('history-status').textContent='读取失败，可点击刷新重试';notify(error.message,true);}return null;}
  }
  function earlierButton(){
    $('history-earlier')?.remove();if(!before)return;
    const b=node('button','加载更早消息','secondary-button history-earlier');b.id='history-earlier';b.type='button';b.onclick=loadEarlier;$('chat-log').prepend(b);
  }
  async function openConversation(id,saveDraft=true){
    if(!idle())return;if(saveDraft)rememberDraft();loading=true;onLock(true);sync();
    try{
      const data=await api('/conversations/'+encodeURIComponent(id)+'?latest=true&limit=50');
      await onOpen(data,{older:false});before=data.next_before;ui.current=id;onTitle(data.title);
      setDraft(data.archived?'':String(ui.drafts[id]||''));persist();earlierButton();
      if(matchMedia('(max-width:800px)').matches)expand(false);
    }catch(error){notify(error.message,true);}
    finally{loading=false;onLock(false);sync();}
  }
  async function loadEarlier(){
    if(!idle()||!before||!current())return;
    loading=true;onLock(true);sync();
    const log=$('chat-log'),height=log.scrollHeight,top=log.scrollTop;
    try{const data=await api('/conversations/'+encodeURIComponent(current())+'?before='+before+'&limit=50');await onOpen(data,{older:true});before=data.next_before;earlierButton();log.scrollTop=top+log.scrollHeight-height;}
    catch(error){notify(error.message,true);}finally{loading=false;onLock(false);sync();}
  }
  async function changed(id){
    if(!scope)return;
    // A successful first turn replaces the new-conversation draft key.
    if(id&&ui.current!==id){delete ui.drafts.new;ui.current=id;}
    ui.drafts[id||'new']=getDraft().slice(0,4000);persist();
    await refresh();
  }
  function startNew(){rememberDraft();ui.current=null;before=null;onTitle('技术支持助手');persist();setDraft(String(ui.drafts.new||''));render();}
  $('toggle-history').onclick=()=>{expand(!ui.expanded);if(ui.expanded)$('history-refresh').focus();};
  $('history-close').onclick=()=>{expand(false);$('toggle-history').focus();};$('history-backdrop').onclick=$('history-close').onclick;
  $('history-new').onclick=()=>$('new-conversation').click();
  $('history-refresh').onclick=()=>{if(idle())refresh();};$('history-more').onclick=()=>{if(idle())refresh(true);};
  for(const [id,value] of [['history-active',false],['history-archived',true]])$(id).onclick=()=>{if(!idle())return;archived=value;items=[];cursor=null;render();refresh();};
  $('history-edit-close').onclick=()=>$('history-edit-dialog').close();
  $('history-edit-form').onsubmit=async event=>{
    event.preventDefault();if(!idle()||!editing||!$('history-edit-form').reportValidity())return;
    const title=$('history-edit-title').value.trim();if(!title){$('history-edit-error').textContent='请输入会话名称。';return;}
    loading=true;onLock(true);sync();
    try{await api('/conversations/'+encodeURIComponent(editing.conversation_id),{method:'PATCH',body:JSON.stringify({title})});if(current()===editing.conversation_id)onTitle(title);$('history-edit-dialog').close();await refresh();}
    catch(error){$('history-edit-error').textContent=error.message;}finally{loading=false;onLock(false);sync();}
  };
  $('history-archive').onclick=async()=>{
    if(!idle()||!editing)return;const item=editing;
    loading=true;onLock(true);sync();
    try{
      await api('/conversations/'+encodeURIComponent(item.conversation_id),{method:'PATCH',body:JSON.stringify({archived:!item.archived})});
      $('history-edit-dialog').close();
      if(current()===item.conversation_id){const data=await api('/conversations/'+encodeURIComponent(item.conversation_id)+'?latest=true&limit=50');await onOpen(data,{older:false});before=data.next_before;earlierButton();onTitle(data.title);}
      await refresh();
    }catch(error){$('history-edit-error').textContent=error.message;}finally{loading=false;onLock(false);sync();}
  };
  $('message').addEventListener('input',rememberDraft);
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&ui.expanded&&!document.querySelector('dialog[open]')){expand(false);$('toggle-history').focus();}});
  async function init(){
    const data=await refresh();if(!data)return;
    const id=ui.current||current();
    if(id)await openConversation(id,false);else{setDraft(String(ui.drafts.new||''));onTitle('技术支持助手');}
  }
  return {init,refresh,sync,changed,startNew};
}
