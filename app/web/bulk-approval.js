// Each immutable review ID is approved through the existing administrator endpoint.
export function approvalItems(reviews,selectedIds,canApprove){
  if(!canApprove)throw new Error('仅平台管理员可以批量通过审批。');
  const ids=[...new Set(selectedIds)];
  if(!ids.length||ids.length>100)throw new Error('每次请选择 1–100 份待审批申请。');
  return ids.map(id=>{
    const r=reviews.find(x=>x.review_id===id);
    if(!r||r.status!=='pending'||r.withdrawn)throw new Error('所选申请已变化、已撤回或不再待审，请刷新审批列表。');
    return {id:r.review_id,name:`${r.title||r.filename} · ${r.label}`,version_id:r.version_id,
      version_label:r.label,reader_user_ids:[...r.user_ids],submitted_by:r.submitted_by,status:'pending'};
  });
}
export async function approveReviewItem(item,comment,api){
  const r=await api(`/document-reviews/${encodeURIComponent(item.id)}/approve`,{comment});
  if(r.status!=='approved')throw new Error('未确认审批通过，请刷新审批列表检查结果。');
  return {review_id:item.id,reused:!!r.reused,message:r.reused?'该申请已通过，已复用原处理结果':'审批通过，版本及阅读权限已发布'};
}
export function installBulkApproval({api,canApprove,context,labels,start,isBusy,notify}){
  const $=id=>document.getElementById(id);
  const el=(tag,text,cls='')=>{const n=document.createElement(tag);n.textContent=text;n.className=cls;return n;};
  let reviews=[],selected=new Set(),snapshot=null;
  const eligible=()=>reviews.filter(r=>r.status==='pending'&&!r.withdrawn);
  function sync(busy=isBusy()){
    const allowed=canApprove(),rows=eligible();
    $('review-bulk-controls').hidden=!allowed;
    $('approval-selection-count').textContent=`已选 ${selected.size} 份`;
    const master=$('select-pending-reviews');master.checked=!!rows.length&&rows.every(r=>selected.has(r.review_id));
    master.indeterminate=selected.size>0&&!master.checked;master.disabled=busy||!allowed||!rows.length;
    $('open-bulk-approve').disabled=busy||!allowed||!selected.size;
    $('bulk-approve-submit').disabled=busy||!allowed||!snapshot?.items.length;
    for(const box of document.querySelectorAll('.review-select input')){
      box.checked=selected.has(box.dataset.reviewId);box.disabled=busy||!allowed||!rows.some(r=>r.review_id===box.dataset.reviewId);
    }
  }
  function setReviews(next){
    reviews=next;const ids=new Set(eligible().map(r=>r.review_id));
    for(const id of selected)if(!ids.has(id))selected.delete(id);
    if(!canApprove())selected.clear();sync();
  }
  function selectionControl(review){
    const label=el('label','','review-select'),box=document.createElement('input');box.type='checkbox';box.dataset.reviewId=review.review_id;
    box.setAttribute('aria-label',`选择审批：${review.title||review.filename} · ${review.label}`);
    box.checked=selected.has(review.review_id);box.disabled=isBusy()||!canApprove()||!!review.withdrawn;
    box.onchange=()=>{if(box.checked)selected.add(review.review_id);else selected.delete(review.review_id);sync();};
    label.append(box);return label;
  }
  $('select-pending-reviews').onchange=()=>{
    if(isBusy()||!canApprove())return;
    if($('select-pending-reviews').checked)for(const r of eligible())selected.add(r.review_id);else selected.clear();sync();
  };
  $('open-bulk-approve').onclick=()=>{
    if(isBusy())return;
    try{
      const items=approvalItems(reviews,[...selected],canApprove()),ctx={...context()};
      if(!ctx.fingerprint)throw new Error('请先刷新知识库状态。');
      snapshot={items,context:ctx};$('bulk-approve-error').textContent='';$('bulk-approve-comment').value='';
      $('bulk-approve-summary').textContent=`将通过并发布 ${items.length} 份申请。请核对下方版本和各自的阅读范围。`;
      const list=$('bulk-approve-items');list.replaceChildren();const names=labels();
      for(const item of items){
        const row=el('li','');row.append(el('strong',item.name),el('p','发布后可读：'+item.reader_user_ids.map(id=>names[id]||id).join('、'),'field-help'),el('small','提交者：'+(names[item.submitted_by]||item.submitted_by)));
        list.append(row);
      }
      sync();$('bulk-approve-dialog').showModal();
    }catch(error){notify(error.message,true);}
  };
  $('close-bulk-approve').onclick=()=>$('bulk-approve-dialog').close();
  $('bulk-approve-form').onsubmit=event=>{
    event.preventDefault();if(isBusy()||!$('bulk-approve-form').reportValidity()||!snapshot)return;
    if(!canApprove()){$('bulk-approve-error').textContent='仅平台管理员可以批量通过审批。';return;}
    const ctx=context();
    if(ctx.identity!==snapshot.context.identity||ctx.fingerprint!==snapshot.context.fingerprint){$('bulk-approve-error').textContent='身份或工作空间已变化，请关闭并重新选择。';return;}
    const {items,context:fixedContext}=snapshot,comment=$('bulk-approve-comment').value.trim();
    const scopedApi=(path,body)=>api(path,body,'POST',fixedContext);
    $('bulk-approve-dialog').close();snapshot=null;
    start(items,async(item,progress)=>{progress('正在审批并发布…');return approveReviewItem(item,comment,scopedApi);},fixedContext);
  };
  return {sync,setReviews,selectionControl};
}
