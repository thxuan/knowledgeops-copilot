export function createStreamControl(onChange=()=>{}) {
  const controller=new AbortController();
  let stopped=false, canStop=false;
  return {
    controller,
    get stopped(){return stopped;},
    get canStop(){return canStop;},
    phase(stage){canStop=!controller.signal.aborted&&['retrieval','generating','review','validating'].includes(stage);onChange();},
    stop(){
      if(!canStop||controller.signal.aborted)return false;
      stopped=true;canStop=false;controller.abort();onChange();return true;
    }
  };
}
