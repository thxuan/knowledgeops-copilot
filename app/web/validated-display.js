// Only final server results may enter the presentation animation.
export function validatedAnswerText(result){
  if(result?.kind==='agent')return '';
  const response=result?.kind==='question'?result.response:result;
  if(!['answered','needs_clarification'].includes(response?.status))return '';
  const answer=typeof response.answer==='string'?response.answer:'';
  const followUp=typeof response.follow_up==='string'?response.follow_up.trim():'';
  return [answer,followUp?`需要你补充\n${followUp}`:''].filter(Boolean).join('\n\n');
}

export async function animateValidatedText(text,render,{
  isCurrent=()=>true,
  reducedMotion=globalThis.matchMedia?.('(prefers-reduced-motion: reduce)').matches??false,
  now=()=>performance.now(),
  wait=()=>new Promise(resolve=>setTimeout(resolve,16))
}={}){
  if(!text||!isCurrent())return;
  if(reducedMotion){render(text);return;}
  const parts=typeof Intl.Segmenter==='function'
    ?Array.from(new Intl.Segmenter(undefined,{granularity:'grapheme'}).segment(text),part=>part.segment)
    :Array.from(text);
  // Long answers finish within 2.4 seconds in an active tab.
  const duration=Math.min(2400,Math.max(350,parts.length*18)),start=now();
  let shown=0;
  while(shown<parts.length&&isCurrent()){
    const count=Math.min(parts.length,Math.max(1,Math.floor(parts.length*(now()-start)/duration)));
    if(count>shown){shown=count;render(parts.slice(0,shown).join(''));}
    if(shown<parts.length)await wait();
  }
}
