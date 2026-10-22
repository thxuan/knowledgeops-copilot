// Content uses textContent throughout, including untrusted originals and diffs.
export function documentLifecycle({api, action, reload, chunkConfig,labels:labelsFor}) {
  const $=id=>document.getElementById(id);
  let selected=null, state=null, rights=null, restoredFrom=null, busy=false, reviewing=null;
  const status={not_built:'待构建',blocked:'配置待恢复',queued:'后台排队中',cancelled:'构建已取消',building:'构建中',ready:'索引就绪，尚未发布',failed:'构建失败'};
  const eventNames={snapshot:'保存现存原文快照',draft_created:'创建草稿',build_started:'开始构建',build_ready:'索引就绪',build_failed:'构建失败',review_submitted:'提交发布审批',review_approved:'审批通过',review_rejected:'审批退回',review_invalidated:'审批失效',published:'发布版本',withdrawn:'紧急撤回',permissions_changed:'修改访问授权',legacy_update:'旧接口立即更新'};
  const el=(tag,text,cls='')=>{const n=document.createElement(tag);n.textContent=text;n.className=cls;return n;};
  const path=()=>`/documents/${encodeURIComponent(selected.document_id)}`;
  const vpath=id=>`${path()}/versions/${encodeURIComponent(id)}`;
  const time=t=>new Date(t*1000).toLocaleString('zh-CN',{hour12:false});
  function tab(name){
    for(const id of ['current','history','access','events']){
      $('document-tab-'+id).setAttribute('aria-pressed',String(id===name));
      $('document-panel-'+id).hidden=id!==name;
    }
  }
  for(const id of ['current','history','access','events'])$('document-tab-'+id).onclick=()=>tab(id);
  function clear(){selected=state=rights=null;$('version-list').replaceChildren();$('version-body').textContent='';$('version-heading').textContent='选择版本查看原文';$('version-hash').textContent='';$('version-diff').textContent='';$('lifecycle-events').replaceChildren();$('access-explanation').textContent='';$('access-matrix').replaceChildren();$('lifecycle-status').textContent='';}
  function sync(isBusy=busy){
    busy=isBusy;
    $('new-version').disabled=busy||!rights?.can_manage;
    $('withdraw-document').disabled=busy||!rights?.can_manage||!!selected?.withdrawn;
    $('permissions-panel').hidden=!rights?.can_manage;
    for(const button of $('version-list').querySelectorAll('button'))button.disabled=busy||button.dataset.allowed==='false';
    $('document-tab-history').disabled=busy||!rights?.can_view_history;
    $('document-tab-events').disabled=busy||!rights?.can_manage;
  }
  async function showVersion(v){
    const current=state.versions.find(x=>x.current);
    const suffix=current&&current.version_id!==v.version_id?`?compare_to=${encodeURIComponent(current.version_id)}`:'';
    const r=await api(vpath(v.version_id)+suffix);
    $('version-heading').textContent=`${r.label} · ${v.current?'当前版本':'非当前版本，不用于默认问答'}`;
    $('version-body').textContent=r.original_text;
    $('version-diff').textContent=r.diff??'当前版本：选择其他版本查看与当前正文的差异。';
    $('version-hash').textContent=`原文件 SHA-256：${r.file_hash}`;
  }
  function draftDialog(v=null){
    restoredFrom=v?.version_id||null;
    $('draft-form').reset();$('draft-file').required=!v;$('draft-file-field').hidden=!!v;
    $('draft-heading').textContent=v?`以 ${v.label} 创建新版本`:'上传新版本草稿';
    $('draft-help').textContent=v?'复用该版原文并创建新版本；保留所有历史，不恢复历史权限。':'草稿与索引准备期间，当前有效版本继续提供检索。';
    $('draft-error').textContent='';$('draft-dialog').showModal();
  }
  $('new-version').onclick=()=>draftDialog();
  $('close-draft').onclick=()=>$('draft-dialog').close();
  $('draft-form').onsubmit=e=>{
    e.preventDefault();if(!$('draft-form').reportValidity())return;
    action(async()=>{
      try {
        const version=$('draft-label').value.trim(),note=$('draft-note').value.trim();
        if(!version||!note)throw new Error('请填写版本标签和变更说明。');
        const expected_revision=state.revision;
        if(restoredFrom)await api(vpath(restoredFrom)+'/restore',{version,note,expected_revision});
        else {const data=new FormData();data.append('file',$('draft-file').files[0]);data.append('version',version);data.append('note',note);data.append('expected_revision',expected_revision);await api(path()+'/versions',data);}
        $('draft-dialog').close();await reload();tab('history');
      }catch(error){$('draft-error').textContent=error.message;throw error;}
    });
  };
  $('withdraw-document').onclick=()=>{$('withdraw-form').reset();$('withdraw-error').textContent='';$('withdraw-dialog').showModal();};
  $('close-withdraw').onclick=()=>$('withdraw-dialog').close();
  $('withdraw-form').onsubmit=e=>{e.preventDefault();action(async()=>{
    try{const reason=$('withdraw-reason').value.trim();if(!reason)throw new Error('请填写撤回原因。');await api(path()+'/withdraw',{expected_revision:state.revision,reason});$('withdraw-dialog').close();await reload();}
    catch(error){$('withdraw-error').textContent=error.message;throw error;}
  });};
  async function load(d){
    clear();selected=d;tab('current');
    if(d.status!=='succeeded'){sync();return;}
    rights=await api(path()+'/access');
    $('access-explanation').textContent=`${rights.reason} ${rights.history_rule} 工单审核权限不会自动授予文章阅读权限。`;
    const labels=labelsFor();
    const table=el('table','');table.className='access-table';const head=el('tr','');
    for(const text of ['身份','当前正文','历史与发布'])head.append(el('th',text));table.append(head);
    for(const id of rights.can_manage?Object.keys(labels):rights.user_ids){const row=el('tr','');row.append(el('td',labels[id]||id),el('td',rights.withdrawn?'已撤回':rights.user_ids.includes(id)?'直接授权':'未授权'),el('td',id===d.uploaded_by?'上传者可管理':'无历史权限'));table.append(row);}
    $('access-matrix').append(table);
    if(rights.can_manage){
      state=await api(path()+'/versions');
      $('lifecycle-status').textContent=state.withdrawn?'已撤回 · 不参与检索；基于历史创建新草稿，构建并发布后恢复。':`当前原文 ${d.version} · ${state.publication_state==='published'?'已发布':'未发布，准备索引后提交审批'} · 新版本须管理员审批才生效`;
      for(const v of state.versions){
        const row=el('article','','version-card'),heading=el('div','','version-card-heading');
        heading.append(el('strong',v.label),el('span',v.current?(state.withdrawn?'当前版本已撤回':(state.publication_state==='published'?'当前已发布版本':'当前原文 · 未发布')):v.was_published||v.origin==='legacy_snapshot'?'历史版本':status[v.build_status]));
        row.append(heading,el('small',`${time(v.created_at)} · ${v.origin==='legacy_snapshot'?'迁移/现存原文快照时间':v.author}`),el('p',v.note));
        if(v.error_code)row.append(el('p',`构建失败：${v.error_code}。可重试；未切换当前版。`,'error-text'));
        const actions=el('div','','actions');
        const button=(label,allowed,handler)=>{const b=el('button',label,'secondary-button');b.type='button';b.dataset.allowed=String(allowed);b.onclick=()=>action(handler);actions.append(b);};
        button('查看原文与差异',true,()=>showVersion(v));
        button('以此创建新版本',true,()=>draftDialog(v));
        if((!v.current&&!v.was_published&&v.origin!=='legacy_snapshot')||(v.current&&state.publication_state!=='published')){
          button(v.build_status==='failed'?'重试构建':'构建草稿索引',!['building','queued'].includes(v.build_status),async()=>{try{await api(vpath(v.version_id)+'/build',chunkConfig());}finally{await reload();tab('history');}});
          button(v.review?.status==='pending'?'更新审批申请':'提交发布审批',v.build_status==='ready'&&v.base_revision===state.revision,async()=>reviewDialog(v));
          if(v.base_revision!==state.revision)row.append(el('small','发布基线已变化：请以此内容创建新草稿。'));
        }
        if(v.current&&state.publication_state==='published'&&v.build_status==='ready')button('申请调整共享范围',true,async()=>reviewDialog(v));
        if(v.review){const names={pending:'待管理员审批',approved:'审批通过',rejected:'审批退回',invalidated:'审批已失效'};row.append(el('p',(names[v.review.status]||v.review.status)+(v.review.comment?'：'+v.review.comment:'')));}
        row.append(actions);$('version-list').append(row);
      }
      for(const e of state.events){const row=el('li','');let detail={};try{detail=JSON.parse(e.detail);}catch{}
        const readable=e.kind==='permissions_changed'?`授权：[${(detail.before||[]).map(x=>labels[x]||x).join('、')}] → [${(detail.after||[]).map(x=>labels[x]||x).join('、')}]`:detail.note||detail.reason||detail.comment||detail.code||'';
        const version=state.versions.find(v=>v.version_id===e.version_id);
        row.append(el('strong',`${eventNames[e.kind]||e.kind}${version?' · '+version.label:''}`),el('small',`${time(e.created_at)} · ${labels[e.actor]||e.actor}`),el('p',readable));$('lifecycle-events').append(row);
      }
    } else $('lifecycle-status').textContent=`当前可读版本 ${d.version}；历史由上传者与管理员审阅，发布须经管理员审批。`;
    sync();
  }
  function reviewDialog(v){
    reviewing=v;const labels=labelsFor();$('review-users').replaceChildren();
    $('review-version-summary').textContent=`版本 ${v.label} · 审批基线 ${state.revision}`;
    $('review-error').textContent='';
    for(const [id,label] of Object.entries(labels)){
      const box=document.createElement('input');box.type='checkbox';box.value=id;
      box.checked=(rights.user_ids||[]).includes(id)||id===selected.uploaded_by;box.disabled=id===selected.uploaded_by;
      const item=el('label',label);item.prepend(box);$('review-users').append(item);
    }
    $('review-dialog').showModal();
  }
  $('close-review').onclick=()=>$('review-dialog').close();
  $('review-form').onsubmit=e=>{e.preventDefault();action(async()=>{
    try{const user_ids=[...$('review-users').querySelectorAll('input:checked')].map(n=>n.value);
      await api(vpath(reviewing.version_id)+'/review',{expected_revision:state.revision,user_ids});
      $('review-dialog').close();await reload();tab('history');
    }catch(error){$('review-error').textContent=error.message;throw error;}
  });};
  async function inspect(id){const v=state?.versions.find(v=>v.version_id===id);if(v){tab('history');await showVersion(v);}}
  return {load,clear,sync,inspect};
}
