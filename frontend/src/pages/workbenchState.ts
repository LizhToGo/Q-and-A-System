import type { AskRequest, Conversation, ConversationDetail, ConversationScope, StudySuggestionsRequest, StudySuggestionsResponse, ValidateResponse } from '../api'

export const emptyScope: ConversationScope = { course_id: null, topic_id: null, source_document_id: null }
export type ChatThread = {
  key: string
  conversationId: number | null
  scope: ConversationScope
  title: string
  updatedAt: string
  turns: ValidateResponse[]
  draft: string
  error: string
  pending: boolean
  pendingQuestion: string
  loaded: boolean
}
type ChatApi = {
  conversations: () => Promise<Conversation[]>
  conversation: (id: number) => Promise<ConversationDetail>
  ask: (request: AskRequest) => Promise<ValidateResponse>
}
type ChatSnapshot = { threads: Record<string, ChatThread>; activeKey: string; loading: boolean; error: string }
const errorMessage = (reason: unknown, fallback: string) => reason instanceof Error ? reason.message : fallback

// Local identity never changes when the server assigns an ID. In-flight work only
// touches its captured key, even after navigation or creation of another draft.
export class ChatSession {
  private sequence = 0
  private listeners = new Set<() => void>()
  private initialization: Promise<void> | null = null
  private historyRequests = new Set<string>()
  private snapshot: ChatSnapshot

  constructor(private client: ChatApi, private remember: (id: number | null) => void = () => {}) {
    const draft = this.makeDraft()
    this.snapshot = { threads: { [draft.key]: draft }, activeKey: draft.key, loading: true, error: '' }
  }
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  getSnapshot = () => this.snapshot
  private publish(next: ChatSnapshot) { this.snapshot = next; this.listeners.forEach((listener) => listener()) }
  private makeDraft(): ChatThread {
    return { key: `draft-${++this.sequence}`, conversationId: null, scope: { ...emptyScope }, title: '未保存的草稿', updatedAt: '', turns: [], draft: '', error: '', pending: false, pendingQuestion: '', loaded: true }
  }
  private fromConversation(item: Conversation, turns?: ValidateResponse[]): ChatThread {
    return { key: `conversation-${item.id}`, conversationId: item.id, scope: { course_id: item.course_id, topic_id: item.topic_id, source_document_id: item.source_document_id }, title: item.title, updatedAt: item.updated_at, turns: turns ?? [], draft: '', error: '', pending: false, pendingQuestion: '', loaded: turns !== undefined }
  }
  private update(key: string, change: (thread: ChatThread) => ChatThread) {
    const thread = this.snapshot.threads[key]
    if (thread) this.publish({ ...this.snapshot, threads: { ...this.snapshot.threads, [key]: change(thread) } })
  }
  initialize(storedId: number | null): Promise<void> {
    if (this.initialization) return this.initialization
    const initialKey = this.snapshot.activeKey
    this.initialization = (async () => {
      try {
        const conversations = await this.client.conversations()
        const chosen = conversations.find((item) => item.id === storedId)
        const restored = Object.fromEntries(conversations.map((item) => { const thread = this.fromConversation(item); return [thread.key, thread] }))
        this.publish({ ...this.snapshot, threads: { ...this.snapshot.threads, ...restored } })
        // No stored choice means a fresh local draft, not an implicit saved chat.
        if (chosen && this.snapshot.activeKey === initialKey) await this.activate(`conversation-${chosen.id}`)
      } catch (reason) {
        this.publish({ ...this.snapshot, error: errorMessage(reason, '对话加载失败。') })
      } finally { this.publish({ ...this.snapshot, loading: false }) }
    })()
    return this.initialization
  }
  newDraft() {
    const draft = this.makeDraft()
    const threads = { ...this.snapshot.threads }
    for (const [key, thread] of Object.entries(threads)) {
      if (thread.conversationId === null && !thread.pending && !thread.draft && !thread.error) delete threads[key]
    }
    threads[draft.key] = draft
    this.publish({ ...this.snapshot, threads, activeKey: draft.key, error: '' })
    this.remember(null)
  }
  async activate(key: string) {
    const target = this.snapshot.threads[key]
    if (!target) return
    this.publish({ ...this.snapshot, activeKey: key, error: '' })
    this.remember(target.conversationId)
    if (target.loaded || target.conversationId === null || this.historyRequests.has(key)) return
    this.historyRequests.add(key)
    try {
      const detail = await this.client.conversation(target.conversationId)
      this.update(key, (thread) => ({ ...thread, loaded: true, error: '', turns: [...new Map([...detail.turns, ...thread.turns].map((turn) => [turn.answer_id, turn])).values()] }))
    } catch (reason) {
      this.update(key, (thread) => ({ ...thread, error: errorMessage(reason, '历史加载失败。') }))
    } finally { this.historyRequests.delete(key) }
  }
  setQuestion(value: string) { this.update(this.snapshot.activeKey, (thread) => ({ ...thread, draft: value })) }
  setScope(scope: Partial<ConversationScope>) { this.update(this.snapshot.activeKey, (thread) => thread.pending ? thread : ({ ...thread, scope: { ...thread.scope, ...scope } })) }
  async submit(model: { providerId: string; model: string }) {
    const key = this.snapshot.activeKey
    const thread = this.snapshot.threads[key]
    const question = thread.draft.trim()
    if (!question || thread.pending || !thread.loaded) return
    this.update(key, (value) => ({ ...value, pending: true, pendingQuestion: question, error: '', draft: '' }))
    try {
      const result = await this.client.ask({
        ...(thread.conversationId === null ? {} : { conversation_id: thread.conversationId }),
        question, ...thread.scope,
        ...(model.providerId ? { provider_id: model.providerId } : {}),
        ...(model.model.trim() ? { model: model.model.trim() } : {}), use_web: false,
      })
      if (!result.conversation_id || result.conversation_id <= 0) throw new Error('服务未返回有效对话编号，请重试。')
      this.update(key, (value) => ({ ...value, conversationId: result.conversation_id, pending: false, pendingQuestion: '', turns: [...value.turns, { ...result, question }], title: value.turns.length ? value.title : question.replace(/\s+/g, ' ').slice(0, 80), updatedAt: new Date().toISOString() }))
      if (this.snapshot.activeKey === key) this.remember(result.conversation_id)
    } catch (reason) {
      this.update(key, (value) => ({ ...value, pending: false, pendingQuestion: '', draft: value.draft || question, error: errorMessage(reason, '回答生成失败，请稍后重试。') }))
    }
  }
}

type SuggestionsSnapshot = { context: string; suggestions: string[]; loading: boolean; error: string; provider: string; model: string }
export class StudySuggestionsSession {
  private sequence = 0
  private listeners = new Set<() => void>()
  private snapshot: SuggestionsSnapshot = { context: '', suggestions: [], loading: false, error: '', provider: '', model: '' }
  constructor(private request: (input: StudySuggestionsRequest) => Promise<StudySuggestionsResponse>) {}
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  getSnapshot = () => this.snapshot
  private publish(next: SuggestionsSnapshot) { this.snapshot = next; this.listeners.forEach((listener) => listener()) }
  invalidate() { this.sequence += 1 }
  async load(context: string, input: StudySuggestionsRequest) {
    const sequence = ++this.sequence
    const previous = this.snapshot.context === context ? this.snapshot.suggestions : []
    this.publish({ context, suggestions: [], loading: true, error: '', provider: '', model: '' })
    try {
      const result = await this.request({ ...input, exclude: previous })
      if (sequence !== this.sequence) return
      this.publish({ context, suggestions: result.suggestions, loading: false, error: '', provider: result.provider, model: result.model })
    } catch (reason) {
      if (sequence !== this.sequence) return
      this.publish({ context, suggestions: [], loading: false, error: errorMessage(reason, '学习问题生成失败，请换一组重试。'), provider: '', model: '' })
    }
  }
}
