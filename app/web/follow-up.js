// Follow-up questions are separate from cited factual claims and render as plain text.
export function appendFollowUp(container,response,doc=document){
  const question=typeof response?.follow_up==='string'?response.follow_up.trim():'';
  if(!question)return null;
  const box=doc.createElement('section');box.className='answer-follow-up';box.setAttribute('aria-label','需要补充的信息');
  const title=doc.createElement('strong');title.textContent='需要你补充';
  const text=doc.createElement('p');text.textContent=question;
  box.append(title,text);container.append(box);return box;
}
