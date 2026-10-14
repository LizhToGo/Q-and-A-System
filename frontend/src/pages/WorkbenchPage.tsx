import { useEffect, useRef, useState, useSyncExternalStore, type FormEvent } from 'react'
import { api, type Course, type PublicProvider, type SourceDocument, type Topic, type ValidateResponse, type ValidationCheck } from '../api'
import { RichText } from '../components/RichText'
import { EvidenceView } from '../components/EvidenceView'
import { StatusBadge } from '../components/StatusBadge'
import { ModelSelector, type ModelSelection } from '../components/ModelSelector'
import { LocalToolResults } from '../components/LocalToolResults'
import { ChatSession, StudySuggestionsSession, type ChatThread } from './workbenchState'

interface WorkbenchPageProps {
  apiState: 'checking' | 'online' | 'offline'
}
const activeConversationKey = 'knowledge-active-conversation-id'

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
function riskText(risk: string) {
  if (/formula检查invalid|TokenError|SyntaxError/.test(risk)) return '历史公式核验包含解析错误，不能据此认定公式不一致；重新提问可使用修正后的核验。'
  return risk
}
const categoryLabels: Record<string, string> = { text: '概念', formula: '公式', numeric: '数值', cross_modal: '图表' }
const statusLabels: Record<string, string> = { pass: '通过', fail: '不一致', invalid: '无法自动校验', conflict: '存在冲突', unknown: '无法自动确认', not_applicable: '不适用' }

export function WorkbenchPage({ apiState }: WorkbenchPageProps) {
  const [session] = useState(() => new ChatSession(api, (id) => {
    try {
      if (id === null) localStorage.removeItem(activeConversationKey)
      else localStorage.setItem(activeConversationKey, String(id))
    } catch { /* Storage may be unavailable. */ }
  }))
  const snapshot = useSyncExternalStore(session.subscribe, session.getSnapshot)
  const [suggestionsSession] = useState(() => new StudySuggestionsSession(api.studySuggestions))
  const recommendation = useSyncExternalStore(suggestionsSession.subscribe, suggestionsSession.getSnapshot)
  const [suggestionRevision, setSuggestionRevision] = useState(0)
  const [courses, setCourses] = useState<Course[]>([])
  const [topics, setTopics] = useState<Topic[]>([])
  const [documents, setDocuments] = useState<SourceDocument[]>([])
  const [providers, setProviders] = useState<PublicProvider[]>([])
  const [selection, setSelection] = useState<ModelSelection>({ providerId: '', model: '' })
  const [loading, setLoading] = useState(true)
  const [topicsLoading, setTopicsLoading] = useState(false)
  const [scopeError, setScopeError] = useState('')
  const [providerLoading, setProviderLoading] = useState(true)
  const [modelDiscoveryError, setModelDiscoveryError] = useState('')
  const [globalError, setGlobalError] = useState('')
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const providerRequest = useRef(0)
  const current = snapshot.threads[snapshot.activeKey]
  const turns = current.turns
  const question = current.draft
  const submitting = current.pending
  const error = current.error || snapshot.error || globalError || scopeError
  const courseId = current.scope.course_id?.toString() ?? ''
  const topicId = current.scope.topic_id?.toString() ?? ''
  const documentId = current.scope.source_document_id?.toString() ?? ''
  const historyLoading = !current.loaded
  const isDraft = current.conversationId === null && turns.length === 0
  const canRecommend = isDraft && !submitting && !snapshot.loading && (!providerLoading || providers.length > 0) && !!selection.providerId && !!selection.model.trim()
  const suggestionContext = canRecommend ? JSON.stringify([current.key, courseId, topicId, documentId, selection.providerId, selection.model.trim()]) : ''
  const visibleRecommendations = recommendation.context === suggestionContext ? recommendation : null
  const history = Object.values(snapshot.threads).filter((thread) => thread.conversationId !== null).sort((a, b) => b.updatedAt.localeCompare(a.updatedAt) || (b.conversationId ?? 0) - (a.conversationId ?? 0))
  const localDrafts = Object.values(snapshot.threads).filter((thread) => thread.conversationId === null && (thread.draft || thread.pending || thread.error))
  const titleTargets = history.filter((thread) => thread.titleSource === 'pending' && !thread.pending && !thread.deleting).map((thread) => thread.conversationId).join(',')
  const deleteThread = deleteTarget === null ? undefined : snapshot.threads[deleteTarget]

  useEffect(() => {
    if (!snapshot.loading && !providerLoading && titleTargets) void session.generatePendingTitles(selection)
  }, [session, snapshot.loading, snapshot.titlesLoading, providerLoading, titleTargets, selection.providerId, selection.model])

  useEffect(() => {
    let stored: number | null = null
    try { stored = Number(localStorage.getItem(activeConversationKey)) || null } catch { /* Storage may be unavailable. */ }
    void session.initialize(stored)
  }, [session])

  useEffect(() => {
    let active = true
    api.courses().then((data) => { if (active) setCourses(data) })
      .catch((reason: unknown) => { if (active) setGlobalError(reason instanceof Error ? reason.message : '课程加载失败。') })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [])

  useEffect(() => {
    let active = true
    setTopics([]); setDocuments([]); setScopeError('')
    if (!courseId) { setTopicsLoading(false); return () => { active = false } }
    setTopicsLoading(true)
    Promise.all([api.topics(Number(courseId)), api.sourceDocuments(Number(courseId))])
      .then(([nextTopics, nextDocuments]) => { if (active) { setTopics(nextTopics); setDocuments(nextDocuments) } })
      .catch((reason: unknown) => { if (active) setScopeError(reason instanceof Error ? reason.message : '检索范围加载失败。') })
      .finally(() => { if (active) setTopicsLoading(false) })
    return () => { active = false }
  }, [courseId])

  useEffect(() => {
    let active = true
    const refreshProviders = () => {
      const sequence = ++providerRequest.current
      setProviderLoading(true)
      api.publicProviders().then((data) => {
        if (!active || sequence !== providerRequest.current) return
        setProviders(data.providers)
        setSelection((chosen) => {
          const existing = data.providers.find((provider) => provider.id === chosen.providerId)
          if (existing) return { providerId: existing.id, model: chosen.model || existing.model }
          const initial = data.providers.find((provider) => provider.id === data.default_provider_id) ?? data.providers[0]
          return { providerId: initial?.id ?? '', model: initial?.model ?? '' }
        })
      }).catch((reason: unknown) => { if (active && sequence === providerRequest.current) setGlobalError(reason instanceof Error ? reason.message : 'provider 列表加载失败。') })
        .finally(() => { if (active && sequence === providerRequest.current) setProviderLoading(false) })
    }
    refreshProviders()
    window.addEventListener('focus', refreshProviders)
    return () => { active = false; providerRequest.current += 1; window.removeEventListener('focus', refreshProviders) }
  }, [])

  useEffect(() => {
    if (!suggestionContext) { suggestionsSession.invalidate(); return }
    void suggestionsSession.load(suggestionContext, { ...current.scope, provider_id: selection.providerId, model: selection.model.trim() })
    return () => { suggestionsSession.invalidate() }
  }, [suggestionsSession, suggestionContext, suggestionRevision])

  useEffect(() => { scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' }) }, [turns, submitting, snapshot.activeKey])
  const submitForm = (event: FormEvent<HTMLFormElement>) => { event.preventDefault(); void session.submit(selection) }

  return <div className="chat-layout">
    <aside className="chat-sidebar">
      <div className="chat-brand"><span className="chat-brand-mark">答</span><strong>知答</strong></div>
      <button className="new-chat-button" type="button" onClick={() => session.newDraft()} disabled={snapshot.loading}><span>＋</span> 新建对话</button>
      <div className="sidebar-section-label">当前空间</div><div className="sidebar-workspace"><span className="workspace-dot" />教材知识库问答</div>
      {localDrafts.length > 0 && <><div className="sidebar-section-label draft-section-label">本地草稿 · 尚未保存</div><nav className="draft-list" aria-label="本地草稿">{localDrafts.map((thread) => <button type="button" key={thread.key} className={`conversation-button${snapshot.activeKey === thread.key ? ' active' : ''}`} onClick={() => void session.activate(thread.key)} title={thread.draft || thread.pendingQuestion || '提交失败的草稿'}><span className="conversation-title">{thread.pendingQuestion || thread.draft || '提交失败的草稿'}</span>{thread.pending && <span className="conversation-pending" aria-label="回答生成中">…</span>}</button>)}</nav></>}
      <div className="sidebar-section-label history-section-label">历史对话</div>
      <nav className="conversation-list" aria-label="历史对话">{history.map((thread) => <div className={`conversation-row${snapshot.activeKey === thread.key ? ' active' : ''}`} key={thread.key} data-conversation-id={thread.conversationId}><button type="button" className="conversation-button" aria-current={snapshot.activeKey === thread.key ? 'page' : undefined} onClick={() => void session.activate(thread.key)} title={thread.title} disabled={thread.deleting}><span className="conversation-title">{thread.title}</span>{thread.pending && <span className="conversation-pending" aria-label="回答生成中">…</span>}</button><button type="button" className="conversation-delete" aria-label={`删除对话：${thread.title}`} title="删除对话" disabled={thread.pending || thread.deleting} onClick={() => setDeleteTarget(thread.key)}><svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M3 5h14M7 5V3h6v2M5 5l1 12h8l1-12M8 8v6M12 8v6" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" /></svg></button></div>)}{history.length === 0 && <p className="history-empty">首次提问后，对话会保存在这里。</p>}</nav>
      {snapshot.titlesLoading && <p className="history-title-status" role="status">正在根据首轮问答生成标题…</p>}
      {snapshot.titleError && <div className="history-title-error" role="alert"><span>{snapshot.titleError}</span><button type="button" disabled={snapshot.titlesLoading} onClick={() => void session.generatePendingTitles(selection, true)}>重试标题</button></div>}
      <div className="sidebar-spacer" />
      <div className="sidebar-footnote">教材证据可追溯，计算图像由本地工具实际生成。</div><a className="admin-entry" href="/admin#evaluation">管理后台</a><div className={`sidebar-api api-${apiState}`}><span className="api-dot" />{apiState === 'online' ? '后端已连接' : apiState === 'checking' ? '连接检查中' : '后端暂不可用'}</div>
    </aside>
    <main className="chat-main">
      <header className="chat-header"><ModelSelector providers={providers} selection={selection} loading={providerLoading} onSelect={setSelection} onDiscoveryError={setModelDiscoveryError} /><div className="chat-header-actions"><strong>教材问答</strong><a href="/admin#settings">模型配置</a></div></header>
      <div className="chat-scroll" ref={scrollRef} aria-label="对话内容"><div className="chat-content">
        {loading || snapshot.loading ? <div className="chat-loading"><span className="spinner" /> 正在加载对话与知识库…</div> : historyLoading ? <div className="chat-loading">{current.error ? <button type="button" className="suggestion-refresh" onClick={() => void session.activate(current.key)}>重新加载历史</button> : <><span className="spinner" /> 正在加载历史对话…</>}</div> : turns.length === 0 && !submitting ? <section className="chat-welcome"><div className="welcome-mark">知</div><span className="welcome-eyebrow">从教材出发，用计算探索</span><h1>今天想研究什么？</h1><p>提问、推导或绘图，结合课程资料理解每一步。首次发送前，草稿只保留在当前页面。</p>{isDraft && <section className="study-suggestions" aria-label="模型学习问题"><div className="suggestion-heading"><div><strong>探索当前知识库</strong><span>由所选模型根据当前检索范围生成</span></div><button className="suggestion-refresh" type="button" onClick={() => setSuggestionRevision((value) => value + 1)} disabled={!canRecommend || visibleRecommendations?.loading}>换一组</button></div>{!canRecommend ? <p className="suggestion-status" role="status">{providerLoading ? '正在加载模型…' : '请先选择可用模型以生成学习问题。'}</p> : visibleRecommendations?.loading || !visibleRecommendations ? <p className="suggestion-status" role="status"><span className="spinner" /> 正在结合教材生成问题…</p> : visibleRecommendations.error ? <p className="suggestion-error" role="alert">{visibleRecommendations.error}</p> : <><div className="suggestion-grid">{visibleRecommendations.suggestions.map((item) => <button key={item} type="button" onClick={() => session.setQuestion(item)}>{item}<span aria-hidden="true">↗</span></button>)}</div><p className="suggestion-source">{visibleRecommendations.provider} / {visibleRecommendations.model} · 点击填入，不会自动发送</p></>}</section>}</section> : turns.map((turn) => <ChatTurnView key={turn.answer_id} result={turn} />)}
        {submitting && <section className="pending-turn" aria-label="正在生成回答"><div className="chat-message user-message"><div className="chat-bubble user-bubble">{current.pendingQuestion}</div></div><div className="chat-message assistant-message"><div className="message-avatar assistant-avatar">答</div><div className="chat-bubble assistant-bubble thinking-bubble" role="status"><span className="thinking-dots"><i /><i /><i /></span> 正在生成回答，包含教材检索、模型处理和本地计算…</div></div></section>}
      </div></div>
      <div className="chat-composer-wrap"><div className="composer-content">
        {error && <div className="chat-error" role="alert">{error}</div>}{modelDiscoveryError && <div className="model-discovery-notice" role="status">{modelDiscoveryError}</div>}
        <form className="chat-composer" onSubmit={submitForm}><textarea aria-label="问题草稿" value={question} onChange={(event) => session.setQuestion(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void session.submit(selection) } }} placeholder="提问概念、推导公式，或描述需要绘制的函数…" rows={2} disabled={submitting || current.deleting || historyLoading || snapshot.loading} />
          <div className="composer-toolbar"><div className="scope-controls" aria-label="知识检索范围"><select aria-label="选择课程" value={courseId} onChange={(event) => session.setScope({ course_id: Number(event.target.value) || null, topic_id: null, source_document_id: null })} disabled={submitting || historyLoading}><option value="">全部课程</option>{courses.map((course) => <option key={course.id} value={course.id}>{course.name}</option>)}</select><select aria-label="选择教材" value={documentId} onChange={(event) => session.setScope({ source_document_id: Number(event.target.value) || null })} disabled={submitting || historyLoading || !courseId || topicsLoading || documents.length === 0}><option value="">全部教材</option>{documents.map((document) => <option key={document.id} value={document.id}>{document.filename} · RAG {Math.round(document.rag_coverage * 100)}%</option>)}</select><select aria-label="选择主题" value={topicId} onChange={(event) => session.setScope({ topic_id: Number(event.target.value) || null })} disabled={submitting || historyLoading || !courseId || topicsLoading}><option value="">{topicsLoading ? '主题加载中…' : '全部主题'}</option>{topics.map((topic) => <option key={topic.id} value={topic.id}>{topic.name}</option>)}</select></div><button className="send-button" type="submit" aria-label="发送问题" disabled={submitting || historyLoading || snapshot.loading || !question.trim()}>{submitting ? '…' : '↑'}</button></div>
        </form><p className="composer-hint">Enter 发送 · Shift + Enter 换行 · 教材引用与本地计算分别展示，请核对适用条件。</p>
      </div></div>
    </main>
    {deleteThread && <DeleteConversationDialog thread={deleteThread} onCancel={() => setDeleteTarget(null)} onDelete={async () => { if (await session.deleteConversation(deleteThread.key)) setDeleteTarget(null) }} />}
  </div>
}

function DeleteConversationDialog({ thread, onCancel, onDelete }: { thread: ChatThread; onCancel: () => void; onDelete: () => Promise<void> }) {
  const dialog = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    const element = dialog.current!
    element.showModal()
    return () => { element.close() }
  }, [])
  return <dialog ref={dialog} className="conversation-delete-dialog" aria-labelledby="delete-conversation-title" onCancel={(event) => { event.preventDefault(); if (!thread.deleting) onCancel() }}>
    <h2 id="delete-conversation-title">删除此对话？</h2>
    <p className="delete-conversation-name">{thread.title}</p>
    <p>这会删除该对话的问答记录及本地计算图像，无法撤销，不会影响教材或其他对话。</p>
    {thread.error && <p className="dialog-error" role="alert">{thread.error}</p>}
    <div className="dialog-actions"><button type="button" onClick={onCancel} disabled={thread.deleting}>取消</button><button type="button" className="delete-confirm" disabled={thread.deleting} onClick={() => void onDelete()}>{thread.deleting ? '正在删除…' : '删除'}</button></div>
  </dialog>
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
  return <article className="chat-turn" data-answer-id={result.answer_id}>
    <div className="chat-message user-message"><div className="message-avatar user-avatar">我</div><div className="chat-bubble user-bubble">{result.question}</div></div>
    <div className="chat-message assistant-message"><div className="message-avatar assistant-avatar">答</div><div className="assistant-content">
      <RichText className="assistant-answer" text={result.answer} />
      <LocalToolResults results={result.tool_results ?? []} />
      <div className="answer-meta"><span className={modelUsed ? 'answer-origin model' : 'answer-origin fallback'}>{modelUsed ? `模型回答${providerName || modelName ? ` · ${providerName}${providerName && modelName ? ' / ' : ''}${modelName}` : ''}` : '本地资料回退'}</span><span>·</span><span>{result.traceability.status === 'traced' ? '已追溯' : '依据不足'}</span><span>RAG 供给 {supplied} · 引用 {cited}</span><span className={`confidence-dot confidence-${result.confidence.level}`} />置信度 {confidenceText(result.confidence.score)}</div>
      {!modelUsed && <div className="answer-fallback-note">模型服务未返回可用回答，已停止展示原始 OCR 片段；下方教材依据仍可展开核对。{fallbackReason && <><br />原因：{fallbackReason}</>}</div>}
      <div className="checks-compact">{result.checks.map((check, index) => <div className="compact-check" key={`${check.category}-${index}`}><StatusBadge status={statusForCheck(check)} /><span>{categoryLabels[check.category] ?? check.category}</span><strong>{statusLabels[check.status] ?? check.status}</strong></div>)}</div>
      {result.risk_flags.length > 0 && <div className="chat-risk"><strong>风险提示</strong>{Array.from(new Set(result.risk_flags.map(riskText))).map((risk) => <span key={risk}>{risk}</span>)}</div>}
      {result.evidence.length > 0 && <details className="chat-evidence" onToggle={(event) => setEvidenceOpen(event.currentTarget.open)}><summary>查看教材依据 · {result.evidence.length} 条</summary>{evidenceOpen && result.evidence.map((evidence) => <EvidenceView key={`${evidence.material_id}-${evidence.chunk_id ?? 'material'}`} evidence={evidence} />)}</details>}
    </div></div>
  </article>
}
