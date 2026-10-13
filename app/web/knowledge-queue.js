// Sequential document work. Stop never aborts an in-flight publication.
export class DocumentQueue {
  constructor(items, work, changed=()=>{}) {
    this.items=items;this.work=work;this.changed=changed;this.running=false;this.stopRequested=false;
  }
  stop(){this.stopRequested=true;this.changed(this);}
  async run(mode='pending') {
    if(this.running)return;
    this.running=true;this.stopRequested=false;
    const targets=this.items.filter(x=>mode==='failed'?x.status==='failed':x.status==='pending');
    this.changed(this);
    try {
      for(const item of targets) {
        if(this.stopRequested)break;
        item.status='running';item.error='';this.changed(this);
        try {
          const result=await this.work(item, text=>{item.detail=text;this.changed(this);});
          item.result=result;item.status=result?.reused?'skipped':'succeeded';item.detail=result?.message||'完成';
        } catch(error) {
          item.status='failed';item.error=error.message;item.detail=error.message;
          if(['WORKSPACE_CHANGED','EMBEDDING_NOT_CONFIGURED'].includes(error.code))this.stopRequested=true;
        }
        this.changed(this);
      }
    } finally {this.running=false;this.changed(this);}
  }
}
