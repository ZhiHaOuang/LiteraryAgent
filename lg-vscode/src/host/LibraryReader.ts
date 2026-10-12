import type { AgentBackend } from '../backend/AgentBackend';
import type { LibraryQuery, LibraryTopic } from '../../shared/protocol';
import type { SessionStore } from './SessionStore';

/** Keep one read in flight and only the latest queued navigation/refresh. */
export class LibraryReader {
  private generation=0;
  private pending?:{generation:number;query:LibraryQuery;key?:string;page:boolean};
  private reading=false;
  private disposed=false;
  private activated=false;
  constructor(private backend:Pick<AgentBackend,'readLibrary'|'readEntry'>,private store:SessionStore){}
  select(query:LibraryQuery){
    if(this.disposed)return;
    this.activated=true;
    const state=this.store.state.library;
    state.query={...query};state.page=null;state.detail=null;state.stale=false;
    this.enqueue(query,undefined,true);
  }
  open(topic:LibraryTopic,key:string){
    if(this.disposed)return;
    this.activated=true;
    const state=this.store.state.library;
    const same=state.query.topic===topic;
    if(!same){state.query={topic,query:'',offset:0};state.page=null;}
    state.detail=null;
    this.enqueue(state.query,key,!same||!state.page);
  }
  invalidate(){
    if(this.disposed)return;
    this.generation++;this.pending=undefined;
    this.store.state.library.stale=true;
    this.store.state.library.loading=false;this.store.publish();
  }
  refresh(){
    if(!this.activated)return;
    const state=this.store.state.library;
    this.enqueue(state.query,state.detail?.key,true);
  }
  private enqueue(query:LibraryQuery,key:string|undefined,page:boolean){
    if(this.disposed)return;
    this.pending={generation:++this.generation,query:{...query},key,page};
    this.store.state.library.loading=true;this.store.state.library.error=null;this.store.publish();
    if(!this.reading)void this.flush();
  }
  private async flush(){
    this.reading=true;
    try{
      while(!this.disposed&&this.pending){
        const job=this.pending;this.pending=undefined;
        try{
          const page=job.page?await this.backend.readLibrary(job.query):undefined;
          if(this.disposed||job.generation!==this.generation)continue;
          const state=this.store.state.library;
          if(page){state.page=page;state.query={topic:page.topic,query:page.query,offset:page.offset};}
          // Clear old previews before reading a changed source, so actions cannot use them.
          state.detail=null;
          const detail=job.key?await this.backend.readEntry(job.query.topic,job.key):null;
          if(this.disposed||job.generation!==this.generation)continue;
          if(detail&&page&&detail.fingerprint!==page.fingerprint)throw Error('Book changed during read');
          state.detail=detail;state.stale=false;state.loading=false;this.store.publish();
        }catch{
          if(this.disposed||job.generation!==this.generation)continue;
          const state=this.store.state.library;
          state.detail=null;state.loading=false;state.stale=true;
          state.error='资料已变更或读取失败，请刷新后重新选择；同时检查配套 LG Python 版本。';
          this.store.publish();
        }
      }
    }finally{this.reading=false;}
  }
  dispose(){this.disposed=true;this.generation++;this.pending=undefined;}
}
