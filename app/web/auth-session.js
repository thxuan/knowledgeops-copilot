// All app modules await this before reading identity. Cookie identity is server-owned.
const initial=await (await globalThis.fetch('/auth/config')).json();
const selector=document.getElementById('identity');
let saved='u_engineer_001';try{saved=sessionStorage.getItem('knowledgeops-identity')||saved;}catch{}
if(initial.mode==='demo'&&selector){
 if(![...selector.options].some(x=>x.value==='u_admin_001'))selector.add(new Option('平台管理员','u_admin_001'));
 if([...selector.options].some(x=>x.value===saved))selector.value=saved;
 selector.addEventListener('change',event=>{event.stopImmediatePropagation();try{sessionStorage.setItem('knowledgeops-identity',selector.value);}catch{}location.reload();},true);
}
let profile=null;
const me=await globalThis.fetch('/auth/me',{headers:initial.mode==='demo'?{'X-Local-User-Id':selector?.value||saved}:{}});
if(!me.ok){location.replace('/login');throw new Error('Authentication required');}
profile=await me.json();
if(initial.mode==='accounts'&&selector){selector.replaceChildren(new Option(profile.display_name,profile.user_id));selector.value=profile.user_id;selector.disabled=true;selector.hidden=true;const name=document.createElement('strong');name.textContent=profile.display_name;selector.parentElement.append(name);}
export const auth={mode:initial.mode,user:profile.user_id,canManage:profile.platform_capabilities.includes('manage_platform'),ticketCapabilities:profile.ticket_capabilities};
export async function authenticatedFetch(input,options={}){
 const headers=new Headers(options.headers||{});
 if(initial.mode==='accounts'&&!['GET','HEAD','OPTIONS'].includes((options.method||'GET').toUpperCase()))headers.set('X-CSRF-Token',profile.csrf_token);
 const response=await globalThis.fetch(input,{...options,headers,credentials:'same-origin'});
 if(initial.mode==='accounts'&&response.status===401)location.replace('/login');
 return response;
}
for(const link of document.querySelectorAll('a[href="/settings"]'))link.hidden=!auth.canManage;
if(location.pathname==='/settings'&&!auth.canManage){location.replace('/knowledge');throw new Error('Platform administrator required');}
const actions=document.querySelector('.top-actions');
if(actions){
 const badge=document.createElement('small');badge.className='field-help';badge.textContent=initial.mode==='demo'?'本地演示 · 身份可切换':'账号登录';actions.prepend(badge);
 if(initial.mode==='accounts'){
  const logout=document.createElement('button');logout.className='secondary-button';logout.textContent='退出';logout.onclick=async()=>{const r=await authenticatedFetch('/auth/logout',{method:'POST'});if(r.ok){try{sessionStorage.removeItem('knowledgeops-identity');}catch{}location.replace('/login');}};actions.append(logout);
  if(auth.canManage){const link=document.createElement('a');link.href='/admin/accounts';link.textContent='账号管理';actions.prepend(link);}
 }
}
