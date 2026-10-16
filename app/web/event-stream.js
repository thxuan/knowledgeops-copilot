/** Decode SSE across arbitrary UTF-8 and network boundaries. */
export async function readEventStream(response,onEvent){
  if(!response.body)throw new Error('浏览器未提供流式响应。');
  const reader=response.body.getReader(),decoder=new TextDecoder();
  let buffer='',kind='message',data=[],result,complete=false;
  async function dispatch(){
    if(!data.length){kind='message';return;}
    const value=JSON.parse(data.join('\n')),type=kind;kind='message';data=[];
    if(type==='error'){const e=new Error(value.message||'流式请求失败');e.code=value.code;throw e;}
    if(type==='result'){result=value;complete=true;}
    await onEvent(type,value);
  }
  async function line(text){
    if(!text){await dispatch();return;}
    if(text.startsWith(':'))return;
    const i=text.indexOf(':'),key=i<0?text:text.slice(0,i),value=i<0?'':text.slice(i+1).replace(/^ /,'');
    if(key==='event')kind=value;
    if(key==='data')data.push(value);
  }
  try{
    while(true){
      const next=await reader.read();buffer+=decoder.decode(next.value,{stream:!next.done});
      let index;
      while((index=buffer.indexOf('\n'))>=0){
        await line(buffer.slice(0,index).replace(/\r$/,''));buffer=buffer.slice(index+1);
      }
      if(next.done){if(buffer)await line(buffer.replace(/\r$/,''));await dispatch();break;}
    }
    if(!complete)throw new Error('连接已中断，回答未完成，请重试。');
    return result;
  }finally{
    try{await reader.cancel();}finally{reader.releaseLock();}
  }
}
