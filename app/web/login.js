const form=document.getElementById('login-form'),message=document.getElementById('login-message');
const mode=await (await fetch('/auth/config')).json();
if(mode.mode==='demo'){form.hidden=true;message.textContent='当前是本地演示模式，无需账号密码。启用账号模式前，请在本机运行管理员初始化命令。';}
if(mode.setup_required){form.hidden=true;message.textContent='管理员尚未初始化。请在服务主机运行 scripts/manage_accounts.py，设置管理员密码后刷新；网页不提供匿名创建管理员入口。';}
form.onsubmit=async e=>{e.preventDefault();const button=form.querySelector('button');button.disabled=true;message.textContent='正在登录…';
 try{const r=await fetch('/auth/login',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'KnowledgeOps'},body:JSON.stringify({username:document.getElementById('username').value,password:document.getElementById('password').value})});const body=await r.json();if(!r.ok)throw new Error(body.error?.message||'登录失败');document.getElementById('password').value='';location.replace('/');}
 catch(error){message.textContent=error.message;}finally{button.disabled=false;}
};
