// Successful jobs can be dismissed locally without deleting durable work or artifacts.
export class CompletedJobHistory {
  constructor(storage,user){
    this.storage=storage;this.key='knowledgeops:index-jobs:dismissed:'+user;this.dismissed=new Set();
    try{const ids=JSON.parse(storage.getItem(this.key)||'[]');if(Array.isArray(ids))this.dismissed=new Set(ids.filter(x=>typeof x==='string'));}catch{}
  }
  save(){try{this.storage.setItem(this.key,JSON.stringify([...this.dismissed]));}catch{}}
  clearCompleted(jobs){
    let count=0;
    for(const job of jobs)if(job.status==='succeeded'&&!this.dismissed.has(job.job_id)){this.dismissed.add(job.job_id);count++;}
    this.save();return count;
  }
  visible(jobs){return jobs.filter(job=>job.status!=='succeeded'||!this.dismissed.has(job.job_id));}
  restore(){this.dismissed.clear();this.save();}
}
