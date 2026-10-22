import {auth,authenticatedFetch as fetch} from './auth-session.js';
const $=id=>document.getElementById(id);let busy=false;
async function api(path,options={}){const r=await fetch(path,{...options,headers:{'X-Local-User-Id':auth.user,...(options.body?{'Content-Type':'application/json'}:{})}});const data=await r.json();if(!r.ok)throw new Error(data.error?.message||'操作失败');return data;}
async function load(){
 if(!auth.canManage||auth.mode!=='accounts'){$('account-form').hidden=true;$('account-message').textContent='需要启用账号模式并使用平台管理员登录。';return;}
 const r=await api('/auth/accounts');$('account-list').replaceChildren();
 for(const account of r.items){const row=document.createElement('article');row.className='panel';const title=document.createElement('strong');title.textContent=`${account.display_name} · ${account.username}`;const detail=document.createElement('p');detail.textContent=`${account.user_id} · ${account.enabled?'已启用':'已停用'}${account.platform_capabilities.includes('manage_platform')?' · 平台管理员':''}`;const button=document.createElement('button');button.className='secondary-button';button.textContent=account.enabled?'停用账号':'启用账号';button.disabled=account.user_id===auth.user;button.onclick=async()=>{if(busy)return;busy=true;button.disabled=true;try{await api(`/auth/accounts/${encodeURIComponent(account.user_id)}/enabled`,{method:'PUT',body:JSON.stringify({enabled:!account.enabled})});await load();}catch(error){$('account-message').textContent=error.message;}finally{busy=false;}};row.append(title,detail,button);$('account-list').append(row);}
}
$('account-form').onsubmit=async e=>{e.preventDefault();if(busy)return;busy=true;$('account-submit').disabled=true;
 try{await api('/auth/accounts',{method:'POST',body:JSON.stringify({username:$('account-username').value,password:$('account-password').value,display_name:$('account-name').value,role:$('account-role').value,...($('account-id').value?{user_id:$('account-id').value}:{})})});$('account-password').value='';$('account-message').textContent='账号已创建。文档阅读仍需单独授权。';await load();}
 catch(error){$('account-message').textContent=error.message;}finally{busy=false;$('account-submit').disabled=false;}
};
load().catch(error=>$('account-message').textContent=error.message);
