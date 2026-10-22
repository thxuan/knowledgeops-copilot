// Batch submission reuses the single-document review checks; publication stays separate.
export function reviewCandidates(state) {
  return state.versions.filter(v=>v.build_status==='ready'&&v.base_revision===state.revision&&
    !v.was_published&&v.origin!=='legacy_snapshot'&&!(v.current&&state.publication_state==='published'));
}
export async function prepareReviewBatch(documents,api,progress=()=>{}) {
  if(!documents.length||documents.length>100)throw new Error('每次请选择 1–100 份文档提交审批。');
  const items=[];
  for(const doc of documents){
    const item={id:doc.document_id,name:doc.title||doc.filename,owner:doc.uploaded_by,status:'pending',candidates:[]};
    progress(`正在检查 ${items.length+1}/${documents.length} 份文档…`);
    try{
      if(!doc.can_manage||doc.withdrawn||doc.status!=='succeeded')throw new Error('无管理权限、已撤回或尚未解析完成。');
      const state=await api(`/documents/${encodeURIComponent(item.id)}/versions`);
      if(state.withdrawn)throw new Error('文档已撤回，请单独处理恢复。');
      if(state.versions.some(v=>v.review?.status==='pending'))throw new Error('已有待审批申请，保留原申请；如需修改请逐份操作。');
      item.expected_revision=state.revision;item.candidates=reviewCandidates(state);
      if(!item.candidates.length)throw new Error('没有可提交的就绪草稿；请检查构建状态和版本基线。');
      if(item.candidates.length===1)item.version_id=item.candidates[0].version_id;
    }catch(error){
      if(error.code==='WORKSPACE_CHANGED')throw error;
      item.status='skipped';item.detail=error.message;
    }
    items.push(item);
  }
  return items;
}
export async function submitReviewItem(item,readers,api) {
  const version=item.candidates.find(v=>v.version_id===item.version_id);
  if(!version)throw new Error('请选择要提交审批的具体草稿版本。');
  const users=[...new Set([...readers,item.owner].filter(Boolean))].sort();
  if(!users.length)throw new Error('请选择阅读账号。');
  const path=`/documents/${encodeURIComponent(item.id)}/versions`;
  const state=await api(path);
  if(state.withdrawn||state.revision!==item.expected_revision)throw new Error('文档已变化，请刷新后重新选择。');
  const current=reviewCandidates(state).find(v=>v.version_id===item.version_id);
  if(!current||current.build_updated_at!==version.build_updated_at)throw new Error('草稿索引已变化或不可提交，请刷新后重新选择。');
  const pending=state.versions.find(v=>v.review?.status==='pending');
  if(pending&&(pending.version_id!==item.version_id||JSON.stringify([...pending.review.user_ids].sort())!==JSON.stringify(users)))
    throw new Error('已有其他待审批申请，未覆盖；请刷新后逐份检查。');
  const review=await api(`${path}/${encodeURIComponent(item.version_id)}/review`,{expected_revision:item.expected_revision,user_ids:users});
  return {review_id:review.review_id,message:'已提交审批，等待管理员通过后发布'};
}
export function installBulkReview({api,action,selection,context,labels,start,isBusy}) {
  const $=id=>document.getElementById(id);
  const el=(tag,text,cls='')=>{const n=document.createElement(tag);n.textContent=text;n.className=cls;return n;};
  let plan=[],snapshot=null;
  const scopedApi=(ctx)=>(path,body,method)=>api(path,body,method,ctx);
  function sync(busy=isBusy()){
    $('bulk-review-submit').disabled=busy||!plan.some(x=>x.status==='pending');
    for(const control of $('bulk-review-items').querySelectorAll('select'))control.disabled=busy;
    for(const control of $('bulk-review-users').querySelectorAll('input'))control.disabled=busy;
  }
  $('open-bulk-review').onclick=()=>{
    const chosen=selection();
    action(async()=>{
      plan=[];snapshot={...context()};
      if(!snapshot.fingerprint)throw new Error('请先刷新知识库状态。');
      $('bulk-review-error').textContent='';$('bulk-review-items').replaceChildren();$('bulk-review-users').replaceChildren();
      $('bulk-review-summary').textContent='正在检查草稿索引…';$('bulk-review-dialog').showModal();
      try{
        plan=await prepareReviewBatch(chosen,scopedApi(snapshot),text=>$('bulk-review-summary').textContent=text);
        const ready=plan.filter(x=>x.status==='pending').length;
        $('bulk-review-summary').textContent=`已选 ${plan.length} 份 · 可提交 ${ready} 份 · 跳过 ${plan.length-ready} 份。请核对下方版本并选择统一阅读范围。`;
        for(const [id,label] of Object.entries(labels())){
          const box=document.createElement('input');box.type='checkbox';box.value=id;box.checked=id===snapshot.identity;
          const row=el('label',label);row.prepend(box);$('bulk-review-users').append(row);
        }
        for(const item of plan){
          const row=el('li','');row.append(el('strong',item.name));
          if(item.status==='pending'){
            const field=el('label','提交版本','field');const select=document.createElement('select');select.required=true;
            select.setAttribute('aria-label',item.name+'的审批版本');
            select.append(new Option('请选择草稿版本',''),...item.candidates.map(v=>new Option(v.label,v.version_id)));
            select.value=item.version_id||'';select.onchange=()=>item.version_id=select.value;field.append(select);row.append(field);
          }else row.append(el('p','跳过：'+item.detail,'field-help'));
          $('bulk-review-items').append(row);
        }
      }catch(error){$('bulk-review-error').textContent=error.message;throw error;}
    });
  };
  $('close-bulk-review').onclick=()=>$('bulk-review-dialog').close();
  $('bulk-review-form').onsubmit=event=>{
    event.preventDefault();if(isBusy()||!$('bulk-review-form').reportValidity())return;
    const ready=plan.filter(x=>x.status==='pending');if(!ready.length)return;
    if(ready.some(x=>!x.version_id)){$('bulk-review-error').textContent='请选择每份文档要提交的草稿版本。';return;}
    const readers=[...$('bulk-review-users').querySelectorAll('input:checked')].map(n=>n.value);
    if(!readers.length&&ready.some(x=>!x.owner)){$('bulk-review-error').textContent='请选择阅读账号。';return;}
    const current=context();
    if(current.identity!==snapshot.identity||current.fingerprint!==snapshot.fingerprint){$('bulk-review-error').textContent='身份或工作空间已变化，请关闭并重新打开。';return;}
    const items=plan.map(x=>({...x,candidates:x.candidates.map(v=>({...v}))})),ctx={...snapshot};
    $('bulk-review-dialog').close();
    start(items,async(item,progress)=>{progress('正在提交审批…');return submitReviewItem(item,readers,scopedApi(ctx));},ctx);
  };
  return {sync};
}
