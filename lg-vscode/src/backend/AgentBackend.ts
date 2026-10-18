import type { Approval, BackendEvent, Item, Session, LibraryQuery, LibraryPage, LibraryDetail, LibraryTopic, WritingAction, Stage } from '../../shared/protocol';

export interface AgentBackend {
  connect(): Promise<void>;
  list(): Promise<Session[]>;
  create(): Promise<Session>;
  read(sessionId: string): Promise<Item[]>;
  resume(sessionId: string): Promise<void>;
  run(sessionId: string, text: string, stage?:Stage,authorText?:string): Promise<void>;
  cancel(sessionId: string): Promise<void>;
  approve(requestId: string, decision: Approval['choices'][number]): Promise<void>;
  answer(requestId: string, answers: Record<string, string>): Promise<void>;
  refreshBook(): Promise<void>;
  readLibrary(query:LibraryQuery):Promise<LibraryPage>;
  readEntry(topic:LibraryTopic,key:string):Promise<LibraryDetail>;
  prepareWriting(topic:LibraryTopic,key:string,action:WritingAction,fingerprint:string):Promise<string>;
  subscribe(listener: (event: BackendEvent) => void): () => void;
  dispose(): Promise<void>;
}

export abstract class EventBackend {
  private listeners = new Set<(event: BackendEvent) => void>();
  subscribe(listener: (event: BackendEvent) => void) {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  }
  protected emit(event: BackendEvent) { for (const listener of this.listeners) listener(event); }
}
