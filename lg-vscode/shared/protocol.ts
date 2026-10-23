import { z } from 'zod';

export const CONTRACT_VERSION = 1;
const id = z.string().min(1).max(512);
export const LibraryTopicSchema = z.enum(['characters', 'relationships', 'chapters', 'outlines', 'events', 'timeline', 'world', 'storylines', 'analyses', 'bible', 'pending', 'personal', 'usage']);
export type LibraryTopic = z.infer<typeof LibraryTopicSchema>;
export const WritingActionSchema = z.enum(['generate', 'review', 'adopt', 'select', 'withdraw', 'confirm-knowledge', 'withdraw-knowledge']);
export type WritingAction = z.infer<typeof WritingActionSchema>;
export const LibraryQuerySchema = z.object({topic: LibraryTopicSchema, query: z.string().max(120), offset: z.number().int().nonnegative()}).strict();
export type LibraryQuery = z.infer<typeof LibraryQuerySchema>;
export const LIBRARY_TOPICS: {key: LibraryTopic; title: string; description: string; generate?: boolean}[] = [
  {key:'characters',title:'人物',description:'人物总结、角色与来源',generate:true},
  {key:'relationships',title:'人物关系',description:'关系与相关人物资料',generate:true},
  {key:'chapters',title:'章节与版本',description:'正文、场景与候选版本',generate:true},
  {key:'outlines',title:'全书与分卷规划',description:'大纲与章节目标',generate:true},
  {key:'world',title:'世界与设定',description:'规则、资料与世界观分析',generate:true},
  {key:'events',title:'剧情事件',description:'已保存的剧情进展与证据',generate:true},
  {key:'timeline',title:'时间线',description:'正式事件与场景时间安排'},
  {key:'storylines',title:'故事线与伏笔',description:'主支线、埋设与回收状态',generate:true},
  {key:'analyses',title:'审阅报告',description:'五类审阅、证据与有效性'},
  {key:'bible',title:'已确认事实',description:'经作者确认的事实与来源',generate:true},
  {key:'pending',title:'待确认中心',description:'资料候选、正文候选与设定提案'},
  {key:'personal',title:'写作偏好',description:'CLI 中的个人与写作偏好'},
  {key:'usage',title:'本书模型用量',description:'CLI 已记录的模型用量'},
];
const LibrarySummarySchema = z.object({key:id,title:z.string(),summary:z.string().max(180),status:z.string(),origin:z.string(),actions:z.array(WritingActionSchema)});
export const LibraryPageSchema = LibraryQuerySchema.extend({total:z.number().int().nonnegative(),nextOffset:z.number().int().nonnegative().nullable(),fingerprint:id,entries:z.array(LibrarySummarySchema).max(24)});
export type LibraryPage = z.infer<typeof LibraryPageSchema>;
export const LibraryDetailSchema = z.object({topic:LibraryTopicSchema,key:id,title:z.string(),body:z.string().max(60_000),truncated:z.boolean(),status:z.string(),origin:z.string(),actions:z.array(WritingActionSchema),fingerprint:id,
  versions:z.array(z.object({number:z.number().int().positive(),state:z.string(),reason:z.string(),key:z.string()})).max(50)});
export type LibraryDetail = z.infer<typeof LibraryDetailSchema>;
export const LibraryStateSchema = z.object({query:LibraryQuerySchema,page:LibraryPageSchema.nullable(),detail:LibraryDetailSchema.nullable(),loading:z.boolean(),stale:z.boolean(),error:z.string().nullable()});
export type LibraryState = z.infer<typeof LibraryStateSchema>;
export const emptyLibrary = (): LibraryState => ({query:{topic:'characters',query:'',offset:0},page:null,detail:null,loading:false,stale:false,error:null});
export const Stage = z.enum(['create', 'position', 'plan', 'world', 'scene', 'draft', 'review', 'accept', 'overview']);
export type Stage = z.infer<typeof Stage>;
export const STAGES: { key: Stage; title: string; prompt: string }[] = [
  { key: 'create', title: '创建作品', prompt: '检查并初始化当前作品，确认资料归档位置；不要生成正文。' },
  { key: 'position', title: '创作定位', prompt: '与作者确认原创故事的创作定位、核心冲突和百万字目标，保存为作品规划。' },
  { key: 'plan', title: '全书与分卷', prompt: '根据已有定位规划全书与分卷，只写简洁规划并归档，不生成整书正文。' },
  { key: 'world', title: '人物与世界观', prompt: '整理已有规划的人物、关系与世界观，区分提案和已确认设定，归档资料。' },
  { key: 'scene', title: '第一章与场景', prompt: '规划第一章目标、冲突和场景，说明与全书主线的联系，归档场景计划。' },
  { key: 'draft', title: '正文生成', prompt: '依据已确认计划生成少量第一章原创正文作为候选稿，保留版本与来源，不自动采用。' },
  { key: 'review', title: '审阅修改', prompt: '审阅第一章候选稿的逻辑、人物动机和表达，保存审查及必要修订，不自动采用。' },
  { key: 'accept', title: '确认采用', prompt: '展示第一章待采用版本与审查结论，请求作者明确确认后采用，不绕过过期检查。' },
  { key: 'overview', title: '更新作品概况', prompt: '根据已经保存的作品资料更新全书简介与概况，不把候选稿事实当作正式设定。' },
];
export const ItemSchema = z.object({
  id, runId: id, kind: z.enum(['user', 'assistant', 'tool', 'notice']),
  title: z.string(), text: z.string(), state: z.enum(['running', 'waiting', 'completed', 'failed', 'cancelled']),
  structured: z.unknown().optional(),
  files: z.array(z.string()).optional(),
  diff: z.object({ path: z.string(), before: z.string(), after: z.string(), applied: z.boolean() }).optional(),
});
export type Item = z.infer<typeof ItemSchema>;
export const ApprovalSchema = z.object({
  id, runId: id, itemId: id.optional(), title: z.string(), details: z.string(),
  choices: z.array(z.enum(['accept', 'decline', 'cancel'])).min(1),
  state: z.enum(['pending', 'responding', 'resolved', 'expired']),
});
export type Approval = z.infer<typeof ApprovalSchema>;
export const InputRequestSchema = z.object({ id, runId: id, questions: z.array(z.object({ id, label: z.string(), options: z.array(z.string()) })) });
export type InputRequest = z.infer<typeof InputRequestSchema>;
export const BookSchema = z.object({
  name: z.string(), overview: z.string(), positioning: z.string(), targetWords: z.number().nonnegative(),
  writtenWords: z.number().nonnegative(), acceptedWords: z.number().nonnegative(),
  chapters: z.array(z.object({ id: z.string(), title: z.string(), state: z.string(), words: z.number().nonnegative() })),
  volumes: z.array(z.string()), characters: z.array(z.string()), relationships: z.array(z.string()),
  settings: z.array(z.string()), foreshadowing: z.array(z.string()), tasks: z.array(z.string()),
  pending: z.array(z.string()), source: z.enum(['mock', 'backend']),
});
export type Book = z.infer<typeof BookSchema>;
export const emptyBook = (): Book => ({ name: '尚未读取作品', overview: '', positioning: '', targetWords: 1_000_000,
  writtenWords: 0, acceptedWords: 0, chapters: [], volumes: [], characters: [], relationships: [], settings: [], foreshadowing: [], tasks: [], pending: [], source: 'mock' });
export const SnapshotSchema = z.object({
  version: z.literal(CONTRACT_VERSION), epoch: id, sequence: z.number().int().nonnegative(),
  connection: z.enum(['connecting', 'ready', 'disconnected']), mode: z.enum(['mock', 'codex']),
  sessions: z.array(z.object({ id, title: z.string() })), sessionId: z.string(),
  run: z.object({ id, state: z.enum(['running', 'waiting', 'cancelling', 'completed', 'failed', 'cancelled', 'interrupted']) }).nullable(),
  items: z.array(ItemSchema), approvals: z.array(ApprovalSchema), inputs: z.array(InputRequestSchema), book: BookSchema,
  error: z.string().nullable(), attachment: z.object({ path: z.string(), text: z.string() }).nullable(), library: LibraryStateSchema.default(emptyLibrary),
  configuration:z.object({workspace:z.string(),environment:z.string(),profile:z.string(),model:z.string(),provider:z.string()}).optional(),
});
export type Snapshot = z.infer<typeof SnapshotSchema>;
export const ActiveRunSchema = z.object({id,state:z.enum(['running','waiting','cancelling'])});
export type ActiveRun = z.infer<typeof ActiveRunSchema>;
const fingerprint = z.string().regex(/^[0-9a-f]{64}$/);
export const TaskOptionsSchema = z.object({strategy:z.enum(['polish','chapter']),document:id,
  request:z.string().min(1).max(4000).refine(value=>Boolean(value.trim())),
  passage:z.string().min(1).max(24000).optional(),plot_disagreement:z.boolean().optional(),
  maxModelCalls:z.number().int().min(1).max(2).optional(),
  maxInputChars:z.number().int().min(1).max(48000).optional()}).strict().superRefine((options,ctx)=>{
    if(options.strategy==='polish'&&(!options.passage?.trim()||options.plot_disagreement!==undefined||
        (options.maxModelCalls??1)!==1))
      ctx.addIssue({code:z.ZodIssueCode.custom,message:'Polish requires an exact passage and exactly one model call.'});
    if(options.strategy==='chapter'&&options.passage!==undefined)
      ctx.addIssue({code:z.ZodIssueCode.custom,message:'Chapter creation cannot use a polish selection.'});
  });
export type TaskOptions = z.infer<typeof TaskOptionsSchema>;
const NarrativeOptions = z.object({audience:z.enum(['character','reader']),position:z.number().int(),
  operation:z.enum(['character_next_action','reader_expectation']),
  holder_document:z.union([id,z.number().int().positive()]).optional()}).strict();
export const AuthorTaskRequestSchema = z.discriminatedUnion('action',[
  z.object({action:z.literal('start'),options:TaskOptionsSchema}).strict(),
  z.object({action:z.literal('read'),taskId:id}).strict(),
  z.object({action:z.literal('advance'),taskId:id,fingerprint}).strict(),
  z.object({action:z.literal('select'),taskId:id,optionId:id,fingerprint}).strict(),
  z.object({action:z.literal('reviewAdopt'),document:id,version:z.number().int().positive()}).strict(),
  z.object({action:z.literal('narrative'),options:NarrativeOptions}).strict(),
  z.object({action:z.literal('sources'),document:id,version:z.number().int().positive().optional()}).strict(),
  z.object({action:z.literal('receipt'),operationId:z.string().regex(/^[0-9a-f]{32}$/)}).strict(),
]);
export type AuthorTaskRequest = z.infer<typeof AuthorTaskRequestSchema>;
export const ClientMessageSchema = z.discriminatedUnion('type', [
  z.object({ type: z.literal('ready') }).strict(),
  z.object({type:z.literal('manage'),action:z.enum(['selectBook','models','reconnect','setup'])}).strict(),
  z.object({ type: z.literal('create') }).strict(),
  z.object({ type: z.literal('select'), sessionId: id }).strict(),
  z.object({ type: z.literal('run'), text: z.string().trim().min(1).max(100_000) }).strict(),
  z.object({ type: z.literal('stage'), stage: Stage }).strict(),
  z.object({ type: z.literal('cancel') }).strict(),
  z.object({ type: z.literal('approve'), requestId: id, decision: z.enum(['accept', 'decline', 'cancel']) }).strict(),
  z.object({ type: z.literal('answer'), requestId: id, answers: z.record(z.string().max(10_000)) }).strict(),
  z.object({ type: z.literal('open'), path: z.string().min(1).max(4096) }).strict(),
  z.object({ type: z.literal('diff'), itemId: id }).strict(),
  z.object({ type: z.literal('attach') }).strict(),
  z.object({ type: z.literal('detach') }).strict(),
  z.object({ type: z.literal('copy'), text: z.string().max(1_000_000) }).strict(),
  z.object({ type: z.literal('refreshBook') }).strict(),
  z.object({type:z.literal('library'),query:LibraryQuerySchema}).strict(),
  z.object({type:z.literal('entry'),topic:LibraryTopicSchema,key:id}).strict(),
  z.object({type:z.literal('writingAction'),topic:LibraryTopicSchema,key:z.string().max(512),action:WritingActionSchema,fingerprint:id}).strict(),
  z.object({type:z.literal('boundedTask'),request:AuthorTaskRequestSchema}).strict(),
]);
export type ClientMessage = z.infer<typeof ClientMessageSchema>;
export type Session = { id: string; title: string };
export type BackendEvent =
  | { type: 'backend.status'; status: Snapshot['connection']; error?: string }
  | { type: 'session.snapshot'; sessionId: string; items: Item[]; activeRuns?: ActiveRun[] }
  | { type: 'run.started'; sessionId: string; runId: string; text: string; source?:'retry' }
  | { type: 'assistant.delta'; sessionId: string; runId: string; itemId: string; delta: string }
  | { type: 'item.updated'; sessionId: string; item: Item }
  | { type: 'approval.requested'; sessionId: string; approval: Approval }
  | { type: 'approval.resolved'; sessionId: string; requestId: string }
  | { type: 'approval.expired'; sessionId: string; requestId: string }
  | { type: 'userInput.requested'; sessionId: string; request: InputRequest }
  | { type: 'userInput.resolved'; sessionId: string; requestId: string }
  | { type: 'run.finished'; sessionId: string; runId: string; status: 'completed' | 'failed' | 'cancelled' | 'interrupted'; error?: string }
  | { type: 'book.updated'; book: Book }
  | { type: 'error'; message: string };

export const BackendEventSchema: z.ZodType<BackendEvent> = z.discriminatedUnion('type', [
  z.object({type:z.literal('backend.status'),status:z.enum(['connecting','ready','disconnected']),error:z.string().optional()}),
  z.object({type:z.literal('session.snapshot'),sessionId:id,items:z.array(ItemSchema),activeRuns:z.array(ActiveRunSchema).optional()}),
  z.object({type:z.literal('run.started'),sessionId:id,runId:id,text:z.string(),source:z.literal('retry').optional()}),
  z.object({type:z.literal('assistant.delta'),sessionId:id,runId:id,itemId:id,delta:z.string()}),
  z.object({type:z.literal('item.updated'),sessionId:id,item:ItemSchema}),
  z.object({type:z.literal('approval.requested'),sessionId:id,approval:ApprovalSchema}),
  z.object({type:z.literal('approval.resolved'),sessionId:id,requestId:id}),
  z.object({type:z.literal('approval.expired'),sessionId:id,requestId:id}),
  z.object({type:z.literal('userInput.requested'),sessionId:id,request:InputRequestSchema}),
  z.object({type:z.literal('userInput.resolved'),sessionId:id,requestId:id}),
  z.object({type:z.literal('run.finished'),sessionId:id,runId:id,status:z.enum(['completed','failed','cancelled','interrupted']),error:z.string().optional()}),
  z.object({type:z.literal('book.updated'),book:BookSchema}),
  z.object({type:z.literal('error'),message:z.string()}),
]);
