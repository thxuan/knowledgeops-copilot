export function createScrollFollower(container, jump, now=()=>performance.now()) {
  let pinned=true, lastInput=-Infinity;
  function updateJump() {
    jump.hidden=pinned||container.scrollHeight-container.scrollTop-container.clientHeight<=120;
  }
  function markInput(event) {
    if(event.type==='keydown'&&!['ArrowUp','ArrowDown','PageUp','PageDown','Home','End',' '].includes(event.key))return;
    lastInput=now();pinned=false;updateJump();
  }
  function follow() {
    if(pinned&&now()-lastInput>150)container.scrollTop=container.scrollHeight;
    updateJump();
  }
  function reset() {
    pinned=true;lastInput=-Infinity;container.scrollTop=container.scrollHeight;updateJump();
  }
  container.addEventListener('scroll',()=>{
    pinned=container.scrollHeight-container.scrollTop-container.clientHeight<=40;
    updateJump();
  },{passive:true});
  for(const type of ['wheel','touchmove','keydown'])container.addEventListener(type,markInput,{passive:true});
  jump.addEventListener('click',reset);
  return {follow,reset};
}
