import { describe, expect, it, vi } from 'vitest'
import type { AskRequest, Conversation, ConversationDetail, LocalToolResult, StudySuggestionsRequest, StudySuggestionsResponse, ValidateResponse } from '../api'
import { ChatSession, StudySuggestionsSession } from './workbenchState'

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const historyItem: Conversation = { id: 8, title: '已有问题', course_id: 1, topic_id: 2, source_document_id: 3, created_at: '2026-01-01', updated_at: '2026-01-01' }
function answer(conversationId = 42, answerId = 1): ValidateResponse {
  return { answer_id: answerId, conversation_id: conversationId, question: '问题', answer: '回答', checks: [], evidence: [], confidence: { level: 'high', score: .9, reasons: [] }, risk_flags: [], traceability: {}, agent_trace: [] }
}
function client() {
  return { conversations: vi.fn(async (): Promise<Conversation[]> => []), conversation: vi.fn(async (): Promise<ConversationDetail> => ({ ...historyItem, turns: [answer(8)] })), ask: vi.fn(async (_request: AskRequest) => answer()) }
}
const model = { providerId: 'provider-a', model: 'model-a' }

describe('local chat lifecycle', () => {
  it('keeps initialization and repeated reset entirely unsaved, with no empty history entries', async () => {
    const api = client()
    const session = new ChatSession(api)
    await Promise.all([session.initialize(null), session.initialize(null)])
    session.newDraft(); session.newDraft(); session.newDraft()
    expect(api.conversations).toHaveBeenCalledTimes(1)
    expect(api.ask).not.toHaveBeenCalled()
    expect(Object.values(session.getSnapshot().threads)).toHaveLength(1)
    expect(Object.values(session.getSnapshot().threads).filter((thread) => thread.conversationId !== null)).toHaveLength(0)
    session.setQuestion('尚未提交'); session.setScope({ course_id: 2 })
    expect(api.ask).not.toHaveBeenCalled()
  })

  it('persists the first question once without an ID, then uses the returned ID for subsequent turns', async () => {
    const api = client()
    const pending = deferred<ValidateResponse>()
    api.ask.mockImplementationOnce(() => pending.promise)
    const remembered = vi.fn()
    const session = new ChatSession(api, remembered)
    await session.initialize(null)
    session.setScope({ course_id: 2, topic_id: 5, source_document_id: 9 })
    session.setQuestion('  第一个问题  ')
    const first = session.submit(model)
    await session.submit(model)
    expect(api.ask).toHaveBeenCalledTimes(1)
    expect(api.ask.mock.calls[0][0]).not.toHaveProperty('conversation_id')
    expect(api.ask.mock.calls[0][0]).toMatchObject({ question: '第一个问题', course_id: 2, topic_id: 5, source_document_id: 9, provider_id: 'provider-a', model: 'model-a' })
    pending.resolve(answer())
    await first
    const key = session.getSnapshot().activeKey
    expect(session.getSnapshot().threads[key]).toMatchObject({ conversationId: 42, pending: false, title: '第一个问题', draft: '' })
    expect(remembered).toHaveBeenLastCalledWith(42)
    session.setQuestion('第二个问题')
    await session.submit(model)
    expect(api.ask.mock.calls[1][0].conversation_id).toBe(42)
    expect(session.getSnapshot().threads[key].turns).toHaveLength(2)
  })

  it('attaches a pending first answer only to its captured local draft after reset', async () => {
    const api = client()
    const pending = deferred<ValidateResponse>()
    api.ask.mockImplementationOnce(() => pending.promise)
    const remember = vi.fn()
    const session = new ChatSession(api, remember)
    await session.initialize(null)
    const originalKey = session.getSnapshot().activeKey
    session.setQuestion('第一个草稿'); const sending = session.submit(model)
    session.newDraft()
    const nextKey = session.getSnapshot().activeKey
    session.setQuestion('另一个草稿'); session.setScope({ course_id: 99 })
    pending.resolve(answer(71)); await sending
    expect(session.getSnapshot().activeKey).toBe(nextKey)
    expect(session.getSnapshot().threads[nextKey]).toMatchObject({ conversationId: null, draft: '另一个草稿', scope: { course_id: 99 }, turns: [] })
    expect(session.getSnapshot().threads[originalKey]).toMatchObject({ conversationId: 71, turns: [{ question: '第一个草稿' }] })
    expect(remember).toHaveBeenLastCalledWith(null)
    await session.activate(originalKey)
    expect(session.getSnapshot().threads[originalKey].turns[0].answer).toBe('回答')
    expect(api.conversation).not.toHaveBeenCalled()
  })

  it('isolates pending submissions when switching to saved history', async () => {
    const api = client()
    api.conversations.mockResolvedValue([historyItem])
    const pending = deferred<ValidateResponse>()
    api.ask.mockImplementationOnce(() => pending.promise)
    const session = new ChatSession(api)
    await session.initialize(null)
    const draftKey = session.getSnapshot().activeKey
    session.setQuestion('新问题'); const sending = session.submit(model)
    await session.activate('conversation-8')
    pending.resolve(answer(43)); await sending
    expect(session.getSnapshot().activeKey).toBe('conversation-8')
    expect(session.getSnapshot().threads['conversation-8'].turns.map((turn) => turn.conversation_id)).toEqual([8])
    expect(session.getSnapshot().threads[draftKey].turns.map((turn) => turn.conversation_id)).toEqual([43])
  })

  it('retains failed draft, scope and error while preserving a different active draft', async () => {
    const api = client()
    const pending = deferred<ValidateResponse>()
    api.ask.mockImplementationOnce(() => pending.promise)
    const session = new ChatSession(api)
    await session.initialize(null)
    const failedKey = session.getSnapshot().activeKey
    session.setScope({ course_id: 2 }); session.setQuestion('请保留我的问题')
    const sending = session.submit(model)
    session.newDraft(); const newKey = session.getSnapshot().activeKey
    session.setQuestion('新页面文本')
    pending.reject(new Error('模型暂不可用')); await sending
    expect(session.getSnapshot().threads[failedKey]).toMatchObject({ draft: '请保留我的问题', error: '模型暂不可用', conversationId: null, pending: false, scope: { course_id: 2 } })
    expect(session.getSnapshot().threads[newKey]).toMatchObject({ draft: '新页面文本', error: '', turns: [] })
    await session.activate(failedKey); await session.submit(model)
    expect(api.ask.mock.calls[1][0]).not.toHaveProperty('conversation_id')
    expect(session.getSnapshot().threads[failedKey].error).toBe('')
  })

  it('replays persisted local computation artifacts without regenerating answers or tools', async () => {
    const api = client()
    const tool: LocalToolResult = { id: 'tool-1', tool: 'convolution', status: 'completed', title: '离散卷积', summary: '得到 [3, 10, 8]', parameters: { x: [1, 2], h: [3, 4] }, artifacts: [{ id: 'abcdef', url: '/api/tool-artifacts/abcdef', caption: '卷积结果' }], data: { y: [3, 10, 8] }, notes: ['起始索引 n=0'] }
    api.conversations.mockResolvedValue([historyItem])
    api.conversation.mockResolvedValue({ ...historyItem, turns: [{ ...answer(8), tool_results: [tool] }] })
    const session = new ChatSession(api)
    await session.initialize(8)
    expect(session.getSnapshot().threads['conversation-8'].turns[0].tool_results).toEqual([tool])
    expect(api.ask).not.toHaveBeenCalled()
  })

  it('recovers history loading failure without replacing its draft text', async () => {
    const api = client()
    api.conversations.mockResolvedValue([historyItem])
    api.conversation.mockRejectedValueOnce(new Error('历史读取失败'))
    const session = new ChatSession(api)
    await session.initialize(8)
    expect(session.getSnapshot().threads['conversation-8']).toMatchObject({ loaded: false, error: '历史读取失败' })
    await session.activate('conversation-8')
    expect(session.getSnapshot().threads['conversation-8']).toMatchObject({ loaded: true, error: '' })
  })
})

const recommendationResult = (questions: string[], selectedModel = 'model-a'): StudySuggestionsResponse => ({ suggestions: questions, provider: 'provider-a', model: selectedModel, evidence_ids: [3] })
describe('model study recommendations', () => {
  it('sends all current scope fields and excludes the previous generated group on refresh', async () => {
    const request = vi.fn(async (_input: StudySuggestionsRequest) => recommendationResult(['问题一', '问题二', '问题三']))
    const session = new StudySuggestionsSession(request)
    const input = { course_id: 1, topic_id: 2, source_document_id: 3, provider_id: 'provider-a', model: 'model-a' }
    await session.load('draft-a:model-a:1:2:3', input)
    await session.load('draft-a:model-a:1:2:3', input)
    expect(request.mock.calls[1][0].exclude).toEqual(['问题一', '问题二', '问题三'])
  })

  it('discards stale generation after the current recommendation context changes', async () => {
    const old = deferred<StudySuggestionsResponse>()
    const fresh = deferred<StudySuggestionsResponse>()
    const request = vi.fn((_input: StudySuggestionsRequest) => old.promise).mockImplementationOnce(() => old.promise).mockImplementationOnce(() => fresh.promise)
    const session = new StudySuggestionsSession(request)
    const first = session.load('original', { model: 'old', course_id: 1 })
    const second = session.load('changed', { model: 'new', course_id: 2 })
    fresh.resolve(recommendationResult(['新一', '新二', '新三'], 'new')); await second
    old.resolve(recommendationResult(['旧一', '旧二', '旧三'], 'old')); await first
    expect(session.getSnapshot()).toMatchObject({ context: 'changed', suggestions: ['新一', '新二', '新三'], model: 'new', loading: false })
    expect(request.mock.calls[1][0].exclude).toEqual([])
  })

  it('discards a pending response after leaving the local draft', async () => {
    const pending = deferred<StudySuggestionsResponse>()
    const session = new StudySuggestionsSession(() => pending.promise)
    const loading = session.load('draft', { model: 'model-a' })
    session.invalidate()
    pending.resolve(recommendationResult(['不可见一', '不可见二', '不可见三'])); await loading
    expect(session.getSnapshot().suggestions).toEqual([])
  })

  it('shows the actual generation error with no fixed question fallback, and supports retry', async () => {
    const request = vi.fn(async (_input: StudySuggestionsRequest) => recommendationResult(['真实一', '真实二', '真实三'])).mockRejectedValueOnce(new Error('没有已验证教材范围'))
    const session = new StudySuggestionsSession(request)
    await session.load('scope', { course_id: 9, model: 'model-a' })
    expect(session.getSnapshot()).toMatchObject({ suggestions: [], loading: false, error: '没有已验证教材范围' })
    await session.load('scope', { course_id: 9, model: 'model-a' })
    expect(session.getSnapshot()).toMatchObject({ suggestions: ['真实一', '真实二', '真实三'], error: '' })
  })
})
