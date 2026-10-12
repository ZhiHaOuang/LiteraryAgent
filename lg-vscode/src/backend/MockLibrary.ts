import {LIBRARY_TOPICS,type LibraryTopic,type LibraryQuery,type LibraryPage,type LibraryDetail,type WritingAction} from '../../shared/protocol';

type Row={key:string;title:string;body:string;status:string;actions:WritingAction[]};
export class MockLibrary {
  private revision=1;
  private candidate?:Row;
  private adopted=false;
  get fingerprint(){return 'mock-library-'+this.revision;}
  private rows(topic:LibraryTopic):Row[]{
    if(topic==='pending')return this.candidate?[this.candidate]:[];
    const make=(key:string,title:string,body:string,status='',actions:WritingAction[]=['generate']):Row=>({key,title,body,status,actions});
    if(topic==='characters')return [make('person:lin','林砚','角色：维修学徒\n\n'+(this.adopted?'他选择先切断电源，再救出被困的人。':'他想证明自己的判断，却害怕失去师傅的信任。')+'\n\n来源：第一章场景规划 · v1\n状态：规划资料，不是已确认事实。','planned'),make('person:qiao','乔禾','角色：值班员\n\n她坚持封锁损坏闸门，与林砚的救援计划发生冲突。\n来源：人物资料 · v1','candidate')];
    if(topic==='relationships')return [make('relationship:lin-qiao','林砚与乔禾','林砚 ─ 救援分歧 → 乔禾\n\n来源：人物规划 · v1\n这条关系是规划，不能当作已发生剧情。','planned')];
    if(topic==='chapters')return [make('document:first','第一章：停电之后','审核状态：候选稿\n\n灯熄了。林砚摸索着走向闸门。\n\n来源：first · v1','candidate',['generate','review'])];
    if(topic==='outlines')return [make('outline:first','第一卷：失衡的城市','故事前提：封闭城邦的供能网络失控。\n第一章目标：通过有代价的救援选择建立人物困境。','planned')];
    if(topic==='events')return [make('event:outage','供能中断','钟塔停止，闸门被锁住。\n来源：第一章候选 v1\n状态：候选剧情','candidate')];
    if(topic==='timeline')return [make('time:outage','夜间 · 闸门救援','场景卡时间安排，尚未确认为正式事件。\n目标：救出被困者\n来源：场景规划 v1','planned',[])];
    if(topic==='world')return [make('world:energy','城邦供能规则','城邦依赖统一供能网络。\n来源：世界观资料 v1','candidate')];
    if(topic==='storylines')return [make('thread:mark','修理单上的陌生印记','状态：open\n\n埋设：林砚发现异常印记。\n回收：尚未记录。','open')];
    if(topic==='analyses')return [make('analysis:first:CharacterArc','first | CharacterArc | stale','人物成长审阅\n状态：stale（正文变化后需要重审）\n\n人物选择应在动作前交代动机。\n证据：第一章 v1。','stale',['review'])];
    if(topic==='bible')return [make('fact:energy','供能规则','状态：Canonical（离线示例）\n城邦供能网络每天检修一次。\n来源：已确认设定。','canonical')];
    if(topic==='personal')return [make('personal:writing','个人与写作偏好','回复：适中\n写作偏好：重视人物选择和行为因果。\n来源：CLI 个人偏好（模拟）。','',[])];
    return [make('usage:models','本书用量说明','离线演示不调用模型，无实际用量。真实模式读取 CLI 的已保存用量记录。','',[])];
  }
  async page(query:LibraryQuery):Promise<LibraryPage>{
    const rows=this.rows(query.topic).filter(r=>(r.title+'\n'+r.body).includes(query.query));
    const offset=query.offset<rows.length?query.offset:0;
    return {...query,offset,total:rows.length,nextOffset:offset+24<rows.length?offset+24:null,fingerprint:this.fingerprint,
      entries:rows.slice(offset,offset+24).map(r=>({key:r.key,title:r.title,summary:r.body.replace(/\s+/g,' ').slice(0,180),status:r.status,origin:'离线模拟 · CLI 资料格式',actions:r.actions}))};
  }
  async detail(topic:LibraryTopic,key:string):Promise<LibraryDetail>{
    const row=this.rows(topic).find(r=>r.key===key);if(!row)throw Error('资料已变更，请刷新');
    return {topic,...row,truncated:false,origin:'离线模拟 · CLI 资料格式',fingerprint:this.fingerprint,
      versions:topic==='chapters'?[{number:1,state:'candidate',reason:'演示候选',key:''}]:[]};
  }
  async prepare(topic:LibraryTopic,key:string,action:WritingAction,fingerprint:string){
    if(fingerprint!==this.fingerprint)throw Error('作品已变化，请重新预览');
    const allowed=key?this.rows(topic).find(r=>r.key===key)?.actions:LIBRARY_TOPICS.find(t=>t.key===topic)?.generate?['generate']:[];
    if(!allowed?.includes(action))throw Error('不支持此操作');
    return 'MOCK_LIBRARY:'+JSON.stringify({topic,key,action})+'\n作者发起资料操作；生成保存候选，采用需要另外预览并明确确认。';
  }
  commit(action:string){
    if(action==='generate')this.candidate={key:'candidate:lin:2',title:'林砚 · v2（资料候选）',body:'林砚选择先切断电源，再救出被困的人。\n\n来源：已保存场景规划。\n状态：候选，尚未采用。',status:'candidate',actions:['adopt']};
    else if(action==='adopt'){this.adopted=true;this.candidate=undefined;}
    this.revision++;
  }
}
