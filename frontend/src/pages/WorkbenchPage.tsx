import { useEffect, useRef, useState, type FormEvent } from 'react'
import { api, type Conversation, type ConversationDetail, type ConversationScope, type Course, type PublicProvider, type SourceDocument, type Topic, type ValidateResponse, type ValidationCheck } from '../api'
import { RichText } from '../components/RichText'
import { EvidenceView } from '../components/EvidenceView'
import { StatusBadge } from '../components/StatusBadge'
import { ModelSelector, type ModelSelection } from '../components/ModelSelector'

interface WorkbenchPageProps {
  apiState: 'checking' | 'online' | 'offline'
}

type Thread = {
  summary: Conversation
  turns: ValidateResponse[]
  draft: string
  error: string
  pending: boolean
  loaded: boolean
}

const activeConversationKey = 'knowledge-active-conversation-id'
const emptyScope: ConversationScope = { course_id: null, topic_id: null, source_document_id: null }

function threadFrom(summary: Conversation, turns?: ValidateResponse[]): Thread {
  return { summary, turns: turns ?? [], draft: '', error: '', pending: false, loaded: turns !== undefined }
}

function rememberConversation(id: number) {
  try { localStorage.setItem(activeConversationKey, String(id)) } catch { /* Storage may be unavailable. */ }
}

const suggestions = [
  '解释奈奎斯特采样定理',
  '傅里叶变换和频谱有什么关系？',
  'RC 低通滤波器的截止频率怎么计算？',
]

function confidenceText(value: number | undefined) {
  if (value === undefined || Number.isNaN(value)) return '—'
  return `${Math.round(value <= 1 ? value * 100 : value)}%`
}

function statusForCheck(check: ValidationCheck) {
  if (check.status === 'fail' || check.status === 'conflict') return 'fail'
  if (check.status === 'not_applicable') return 'not_applicable'
  if (check.status === 'unknown' || check.status === 'invalid') return 'warning'
  return 'pass'
}

function categoryLabel(category: string) {
  return { text: '概念', formula: '公式', numeric: '数值', cross_modal: '图表' }[category] ?? category
}

function statusLabel(status: string) {
  return { pass: '通过', fail: '不一致', invalid: '无法自动校验', conflict: '存在冲突', unknown: '无法自动确认', not_applicable: '不适用' }[status] ?? status
}

function riskText(risk: string) {
  if (/formula检查invalid|TokenError|SyntaxError/.test(risk)) return '历史公式核验包含解析错误，不能据此认定公式不一致；重新提问可使用修正后的核验。'
  return risk
}

export function WorkbenchPage({ apiState }: WorkbenchPageProps) {
  const [courses, setCourses] = useState<Course[]>([])
  const [topics, setTopics] = useState<Topic[]>([])
  const [documents, setDocuments] = useState<SourceDocument[]>([])
  const [providers, setProviders] = useState<PublicProvider[]>([])
  const [selection, setSelection] = useState<ModelSelection>({ providerId: '', model: '' })
  const [loading, setLoading] = useState(true)
  const [topicsLoading, setTopicsLoading] = useState(false)
  const [providerLoading, setProviderLoading] = useState(false)
  const [modelDiscoveryError, setModelDiscoveryError] = useState('')
  const [threads, setThreads] = useState<Record<number, Thread>>({})
  const threadRef = useRef<Record<number, Thread>>({})
  const [activeId, setActiveId] = useState<number | null>(null)
  const activeRef = useRef<number | null>(null)
  const [sessionLoading, setSessionLoading] = useState(true)
  const [creating, setCreating] = useState(false)
  const creatingRef = useRef(false)
  const activationSequence = useRef(0)
  const initialization = useRef<Promise<{ conversations: Conversation[]; selected: ConversationDetail }> | null>(null)
  const historyRequests = useRef(new Set<number>())
  const [globalError, setGlobalError] = useState('')
  const current = activeId === null ? undefined : threads[activeId]
  const turns = current?.turns ?? []
  const question = current?.draft ?? ''
  const submitting = current?.pending ?? false
  const error = current?.error || globalError
  const courseId = current?.summary.course_id?.toString() ?? ''
  const topicId = current?.summary.topic_id?.toString() ?? ''
  const documentId = current?.summary.source_document_id?.toString() ?? ''
  const historyLoading = current !== undefined && !current.loaded
  const scrollRef = useRef<HTMLDivElement>(null)
  const providerRequest = useRef(0)

  function updateThread(id: number, update: (thread: Thread) => Thread) {
    const existing = threadRef.current[id]
    if (!existing) return
    threadRef.current = { ...threadRef.current, [id]: update(existing) }
    setThreads(threadRef.current)
  }

  function activate(id: number | null) {
    activeRef.current = id
    setActiveId(id)
    if (id !== null) rememberConversation(id)
  }

  function setQuestion(value: string) {
    if (activeRef.current !== null) updateThread(activeRef.current, (thread) => ({ ...thread, draft: value }))
  }

  function setScope(scope: Partial<ConversationScope>) {
    if (activeRef.current !== null) updateThread(activeRef.current, (thread) => ({ ...thread, summary: { ...thread.summary, ...scope } }))
  }

  useEffect(() => {
    let active = true
    if (!initialization.current) initialization.current = (async () => {
      const conversations = await api.conversations()
      let stored: number | null = null
      try { stored = Number(localStorage.getItem(activeConversationKey)) || null } catch { /* Storage may be unavailable. */ }
      const chosen = conversations.find((item) => item.id === stored) ?? conversations[0]
      if (chosen) return { conversations, selected: await api.conversation(chosen.id) }
      const created = await api.createConversation(emptyScope)
      return { conversations: [created], selected: { ...created, turns: [] } }
    })()
    initialization.current.then(({ conversations, selected }) => {
      if (!active) return
      const restored = Object.fromEntries(conversations.map((item) => [item.id, threadFrom(item)]))
      restored[selected.id] = threadFrom(selected, selected.turns)
      threadRef.current = restored
      setThreads(restored)
      activate(selected.id)
    }).catch((reason: unknown) => {
      if (active) setGlobalError(reason instanceof Error ? reason.message : '对话加载失败。')
    }).finally(() => { if (active) setSessionLoading(false) })
    return () => { active = false }
  }, [])

  useEffect(() => {
    let active = true
    api.courses()
      .then((data) => { if (active) setCourses(data) })
      .catch((reason: unknown) => { if (active) setGlobalError(reason instanceof Error ? reason.message : '课程加载失败。') })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [])

  useEffect(() => {
    let active = true
    setTopics([]); setDocuments([])
    if (!courseId) {
      setTopicsLoading(false)
      return () => { active = false }
    }
    setTopicsLoading(true)
    Promise.all([api.topics(Number(courseId)), api.sourceDocuments(Number(courseId))])
      .then(([nextTopics, nextDocuments]) => {
        if (active) { setTopics(nextTopics); setDocuments(nextDocuments) }
      })
      .catch((reason: unknown) => {
        if (active && activeId !== null) updateThread(activeId, (thread) => ({ ...thread, error: reason instanceof Error ? reason.message : '检索范围加载失败。' }))
      })
      .finally(() => { if (active) setTopicsLoading(false) })
    return () => { active = false }
  }, [courseId, activeId])

  useEffect(() => {
    let active = true
    const refreshProviders = () => {
      const sequence = ++providerRequest.current
      setProviderLoading(true)
      api.publicProviders()
        .then((data) => {
          if (!active || sequence !== providerRequest.current) return
          setProviders(data.providers)
          setSelection((current) => {
            const existing = data.providers.find((provider) => provider.id === current.providerId)
            if (existing) return { providerId: existing.id, model: current.model || existing.model }
            const initial = data.providers.find((provider) => provider.id === data.default_provider_id) ?? data.providers[0]
            return { providerId: initial?.id ?? '', model: initial?.model ?? '' }
          })
        })
        .catch((reason: unknown) => { if (active && sequence === providerRequest.current) setGlobalError(reason instanceof Error ? reason.message : 'provider 列表加载失败。') })
        .finally(() => { if (active && sequence === providerRequest.current) setProviderLoading(false) })
    }
    refreshProviders()
    window.addEventListener('focus', refreshProviders)
    return () => { active = false; providerRequest.current += 1; window.removeEventListener('focus', refreshProviders) }
  }, [])


  useEffect(() => { scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' }) }, [turns, submitting, activeId])

  const selectConversation = (id: number) => {
    activationSequence.current += 1
    activate(id)
    setGlobalError('')
    if (threadRef.current[id].loaded || historyRequests.current.has(id)) return
    historyRequests.current.add(id)
    api.conversation(id).then((detail) => {
      updateThread(id, (thread) => ({
        ...thread,
        summary: detail,
        loaded: true,
        turns: [...new Map([...detail.turns, ...thread.turns].map((turn) => [turn.answer_id, turn])).values()],
      }))
    }).catch((reason: unknown) => {
      updateThread(id, (thread) => ({ ...thread, error: reason instanceof Error ? reason.message : '历史加载失败。' }))
    }).finally(() => { historyRequests.current.delete(id) })
  }

  const submit = async () => {
    const id = activeRef.current
    if (id === null) return
    const thread = threadRef.current[id]
    const trimmed = thread.draft.trim()
    if (!trimmed || thread.pending || !thread.loaded) return
    // Lock the captured thread synchronously, before React schedules a render.
    updateThread(id, (value) => ({ ...value, pending: true, error: '', draft: '' }))
    try {
      const result = await api.ask({
        conversation_id: id,
        question: trimmed,
        course_id: thread.summary.course_id,
        topic_id: thread.summary.topic_id,
        source_document_id: thread.summary.source_document_id,
        ...(selection.providerId ? { provider_id: selection.providerId } : {}),
        ...(selection.model.trim() ? { model: selection.model.trim() } : {}),
        use_web: false,
      })
      updateThread(id, (value) => ({
        ...value,
        turns: [...value.turns, { ...result, question: trimmed }],
        summary: { ...value.summary, title: value.turns.length ? value.summary.title : trimmed.replace(/\s+/g, ' ').slice(0, 80), updated_at: new Date().toISOString() },
      }))
    } catch (reason: unknown) {
      updateThread(id, (value) => ({ ...value, draft: value.draft || trimmed, error: reason instanceof Error ? reason.message : '回答生成失败，请稍后重试。' }))
    } finally {
      updateThread(id, (value) => ({ ...value, pending: false }))
    }
  }

  const submitForm = (event: FormEvent<HTMLFormElement>) => { event.preventDefault(); void submit() }
  const reset = async () => {
    if (creatingRef.current) return
    creatingRef.current = true
    setCreating(true)
    const sequence = ++activationSequence.current
    activate(null)
    setGlobalError('')
    try {
      const conversation = await api.createConversation(emptyScope)
      threadRef.current = { ...threadRef.current, [conversation.id]: threadFrom(conversation, []) }
      setThreads(threadRef.current)
      if (sequence === activationSequence.current) activate(conversation.id)
    } catch (reason: unknown) {
      if (sequence === activationSequence.current) setGlobalError(reason instanceof Error ? reason.message : '新建对话失败。')
    } finally {
      creatingRef.current = false
      setCreating(false)
    }
  }

  return (
    <div className="chat-layout">
      <aside className="chat-sidebar">
        <div className="chat-brand"><span className="chat-brand-mark">答</span><strong>知答</strong></div>
        <button className="new-chat-button" type="button" onClick={() => void reset()} disabled={creating || sessionLoading}><span>＋</span> {creating ? '正在新建…' : '新建对话'}</button>
        <div className="sidebar-section-label">当前空间</div><div className="sidebar-workspace"><span className="workspace-dot" />教材知识库问答</div>
        <div className="sidebar-section-label">历史对话</div>
        <nav className="conversation-list" aria-label="历史对话">{Object.values(threads).sort((a, b) => b.summary.updated_at.localeCompare(a.summary.updated_at) || b.summary.id - a.summary.id).map((thread) => <button type="button" key={thread.summary.id} className={`conversation-button${activeId === thread.summary.id ? ' active' : ''}`} aria-current={activeId === thread.summary.id ? 'page' : undefined} onClick={() => selectConversation(thread.summary.id)} title={thread.summary.title}><span className="conversation-title">{thread.summary.title}</span>{thread.pending && <span className="conversation-pending" aria-label="回答生成中">…</span>}</button>)}</nav><div className="sidebar-spacer" />
        <div className="sidebar-footnote">回答来自本地教材证据，并经过公式、数值与跨模态校验。</div><a className="admin-entry" href="/admin#evaluation"><span>⚙</span> 管理后台</a><div className={`sidebar-api api-${apiState}`}><span className="api-dot" />{apiState === 'online' ? '后端已连接' : apiState === 'checking' ? '连接检查中' : '后端暂不可用'}</div>
      </aside>
      <main className="chat-main">
        <header className="chat-header">
          <ModelSelector providers={providers} selection={selection} loading={providerLoading} onSelect={setSelection} onDiscoveryError={setModelDiscoveryError} />
          <div className="chat-header-actions"><strong>教材问答</strong><a href="/admin#settings">模型配置</a></div>
        </header>
        <div className="chat-scroll" ref={scrollRef}>
          {loading || sessionLoading || historyLoading ? <div className="chat-loading"><span className="spinner" /> 正在加载对话与知识库…</div> : turns.length === 0 ? <section className="chat-welcome"><div className="welcome-mark">✦</div><h1>今天想研究什么？</h1><p>向课程知识库提问，回答会附带教材依据和一致性检查结果。</p><div className="suggestion-grid">{suggestions.map((item) => <button key={item} type="button" onClick={() => setQuestion(item)} disabled={!current}>{item}<span>↗</span></button>)}</div></section> : turns.map((turn) => <ChatTurnView key={turn.answer_id} result={turn} />)}
          {submitting && <div className="chat-message assistant-message"><div className="message-avatar assistant-avatar">答</div><div className="chat-bubble assistant-bubble thinking-bubble"><span className="thinking-dots"><i /><i /><i /></span> 正在检索教材并校验回答…</div></div>}
        </div>
        <div className="chat-composer-wrap">
          {error && <div className="chat-error" role="alert">{error}</div>}
          {modelDiscoveryError && <div className="model-discovery-notice" role="status">{modelDiscoveryError}</div>}
          <form className="chat-composer" onSubmit={submitForm}>
            <textarea value={question} onChange={(event) => setQuestion(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); void submit() } }} placeholder="给知答发送消息…" rows={1} disabled={submitting || !current || historyLoading} />
            <div className="composer-toolbar">
              <div className="scope-controls" aria-label="知识检索范围"><select aria-label="选择课程" value={courseId} onChange={(event) => setScope({ course_id: Number(event.target.value) || null, topic_id: null, source_document_id: null })} disabled={submitting || !current || historyLoading}><option value="">全部课程</option>{courses.map((course) => <option key={course.id} value={course.id}>{course.name}</option>)}</select><select aria-label="选择教材" value={documentId} onChange={(event) => setScope({ source_document_id: Number(event.target.value) || null })} disabled={submitting || historyLoading || !courseId || topicsLoading || documents.length === 0}><option value="">全部教材</option>{documents.map((document) => <option key={document.id} value={document.id}>{document.filename} · RAG {Math.round(document.rag_coverage * 100)}%</option>)}</select><select aria-label="选择主题" value={topicId} onChange={(event) => setScope({ topic_id: Number(event.target.value) || null })} disabled={submitting || historyLoading || !courseId || topicsLoading}><option value="">{topicsLoading ? '主题加载中…' : '全部主题'}</option>{topics.map((topic) => <option key={topic.id} value={topic.id}>{topic.name}</option>)}</select></div>
              <button className="send-button" type="submit" aria-label="发送问题" disabled={submitting || !current || historyLoading || !question.trim()}>{submitting ? '…' : '↑'}</button>
            </div>
          </form><p className="composer-hint">回答可能存在不确定性，请查看下方教材依据和风险提示。</p>
        </div>
      </main>
    </div>
  )
}

function ChatTurnView({ result }: { result: ValidateResponse }) {
  const [evidenceOpen, setEvidenceOpen] = useState(false)
  const generation = result.agent_trace.find((step) => step.step === 'answer_generation')
  const fallbackReason = typeof generation?.failure_reason === 'string' ? generation.failure_reason : typeof generation?.fallback_reason === 'string' ? generation.fallback_reason : ''
  const modelUsed = generation?.status === 'model'
  const providerName = typeof generation?.provider === 'string' ? generation.provider : typeof generation?.provider_name === 'string' ? generation.provider_name : ''
  const modelName = typeof generation?.model === 'string' ? generation.model : ''
  const supplied = typeof generation?.rag_evidence_count === 'number' ? generation.rag_evidence_count : result.evidence.length
  const cited = Array.isArray(generation?.cited_evidence_ids) ? generation.cited_evidence_ids.length : 0
  return <article className="chat-turn">
    <div className="chat-message user-message"><div className="message-avatar user-avatar">我</div><div className="chat-bubble user-bubble">{result.question}</div></div>
    <div className="chat-message assistant-message"><div className="message-avatar assistant-avatar">答</div><div className="assistant-content">
      <RichText className="assistant-answer" text={result.answer} />
      <div className="answer-meta"><span className={modelUsed ? 'answer-origin model' : 'answer-origin fallback'}>{modelUsed ? `模型回答${providerName || modelName ? ` · ${providerName}${providerName && modelName ? ' / ' : ''}${modelName}` : ''}` : '本地资料回退'}</span><span>·</span><span>{result.traceability.status === 'traced' ? '已追溯' : '依据不足'}</span><span>RAG 供给 {supplied} · 引用 {cited}</span><span className={`confidence-dot confidence-${result.confidence.level}`} />置信度 {confidenceText(result.confidence.score)}</div>
      {!modelUsed && <div className="answer-fallback-note">模型服务未返回可用回答，已停止展示原始 OCR 片段；下方教材依据仍可展开核对。{fallbackReason && <><br />原因：{fallbackReason}</>}</div>}
      <div className="checks-compact">{result.checks.map((check, index) => <div className="compact-check" key={`${check.category}-${index}`}><StatusBadge status={statusForCheck(check)} /><span>{categoryLabel(check.category)}</span><strong>{statusLabel(check.status)}</strong></div>)}</div>
      {result.risk_flags.length > 0 && <div className="chat-risk"><strong>风险提示</strong>{Array.from(new Set(result.risk_flags.map(riskText))).map((risk) => <span key={risk}>{risk}</span>)}</div>}
      {result.evidence.length > 0 && <details className="chat-evidence" onToggle={(event) => setEvidenceOpen(event.currentTarget.open)}><summary>查看教材依据 · {result.evidence.length} 条</summary>{evidenceOpen && result.evidence.map((evidence) => <EvidenceView key={`${evidence.material_id}-${evidence.chunk_id ?? 'material'}`} evidence={evidence} />)}</details>}
    </div></div>
  </article>
}
