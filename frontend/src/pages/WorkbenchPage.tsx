import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { api, type Course, type EvidenceItem, type Material, type Topic, type ValidateResponse, type ValidationCheck } from '../api'
import { EmptyState } from '../components/EmptyState'
import { StatusBadge } from '../components/StatusBadge'

interface WorkbenchPageProps { onOpenEvaluation: () => void }

function confidenceText(value: number | undefined) {
  if (value === undefined || Number.isNaN(value)) return '未提供'
  return `${Math.round(value <= 1 ? value * 100 : value)}%`
}

function statusForChecks(checks: ValidationCheck[]) {
  if (!checks.length) return 'unknown'
  if (checks.some((check) => ['fail', 'invalid', 'conflict'].includes(check.status))) return 'fail'
  if (checks.some((check) => ['unknown', 'not_applicable'].includes(check.status))) return 'warning'
  return 'pass'
}

export function WorkbenchPage({ onOpenEvaluation }: WorkbenchPageProps) {
  const [courses, setCourses] = useState<Course[]>([])
  const [topics, setTopics] = useState<Topic[]>([])
  const [materials, setMaterials] = useState<Material[]>([])
  const [courseId, setCourseId] = useState('')
  const [topicId, setTopicId] = useState('')
  const [materialType, setMaterialType] = useState('')
  const [question, setQuestion] = useState('')
  const [answer, setAnswer] = useState('')
  const [result, setResult] = useState<ValidateResponse | null>(null)
  const [searchResults, setSearchResults] = useState<EvidenceItem[]>([])
  const [loading, setLoading] = useState(true)
  const [topicsLoading, setTopicsLoading] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [searchError, setSearchError] = useState('')

  useEffect(() => {
    let active = true
    Promise.all([api.courses(), api.materials()])
      .then(([courseData, materialData]) => {
        if (!active) return
        setCourses(courseData)
        setMaterials(materialData)
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : '基础数据加载失败。')
      })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [])

  useEffect(() => {
    let active = true
    if (!courseId) {
      setTopics([])
      setTopicId('')
      return () => { active = false }
    }
    setTopicsLoading(true)
    setTopicId('')
    api.topics(Number(courseId))
      .then((data) => { if (active) setTopics(data) })
      .catch((reason: unknown) => { if (active) setError(reason instanceof Error ? reason.message : '主题加载失败。') })
      .finally(() => { if (active) setTopicsLoading(false) })
    return () => { active = false }
  }, [courseId])

  const materialTypes = useMemo(() => [...new Set(materials.map((material) => material.material_type))], [materials])
  const checks = result?.checks ?? []

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!question.trim() || !answer.trim()) {
      setError('请先填写问题和答案，再提交校验。')
      return
    }
    setSubmitting(true)
    setError('')
    setSearchError('')
    setResult(null)
    setSearchResults([])
    const selectedType = materialType ? [materialType] : undefined
    const selectedCourse = courseId ? Number(courseId) : undefined
    const selectedTopic = topicId ? Number(topicId) : undefined
    const payload = {
      question: question.trim(),
      answer: answer.trim(),
      ...(selectedCourse === undefined ? {} : { course_id: selectedCourse }),
      ...(selectedTopic === undefined ? {} : { topic_id: selectedTopic }),
    }
    try {
      let matchedMaterials: EvidenceItem[] = []
      try {
        const search = await api.search({ query: question.trim(), ...(selectedCourse === undefined ? {} : { course_id: selectedCourse }), ...(selectedTopic === undefined ? {} : { topic_id: selectedTopic }), ...(selectedType ? { material_types: selectedType } : {}), limit: 8 })
        matchedMaterials = search.results
        setSearchResults(matchedMaterials)
      } catch (reason: unknown) {
        if (selectedType) throw reason
        setSearchError(reason instanceof Error ? reason.message : '证据检索失败，仍可执行答案校验。')
      }
      if (selectedType && !matchedMaterials.length) throw new Error('所选材料类型未找到匹配资料，请更换类型或选择全部类型。')
      const validation = await api.validateAnswer({ ...payload, ...(matchedMaterials.length ? { material_ids: matchedMaterials.map((material) => material.material_id) } : {}) })
      setResult(validation)
      if (validation.evidence.length) setSearchResults(validation.evidence)
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : '答案校验失败，请稍后重试。')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="page-shell">
      <section className="page-heading"><div><p className="eyebrow">ANSWER QUALITY WORKBENCH</p><h1>答案校验工作台</h1><p className="heading-copy">输入题目与作答内容，结合课程资料检查答案的准确性、完整性与风险。</p></div><button className="button button-secondary" onClick={onOpenEvaluation}>查看评估指标 →</button></section>
      {error && <div className="alert alert-error" role="alert">{error}</div>}
      {loading ? <div className="loading-panel"><span className="spinner" /> 正在加载课程与材料…</div> : (
        <form className="workbench-grid" onSubmit={submit}>
          <section className="card editor-card"><div className="card-title-row"><div><span className="step-number">01</span><h2>选择校验范围</h2></div><span className="muted">可选</span></div><div className="field-grid">
            <label className="field"><span>课程</span><select value={courseId} onChange={(event) => setCourseId(event.target.value)}><option value="">全部课程</option>{courses.map((course) => <option key={course.id} value={course.id}>{course.name}</option>)}</select></label>
            <label className="field"><span>主题</span><select value={topicId} onChange={(event) => setTopicId(event.target.value)} disabled={!courseId || topicsLoading}><option value="">{topicsLoading ? '主题加载中…' : '全部主题'}</option>{topics.map((topic) => <option key={topic.id} value={topic.id}>{topic.name}</option>)}</select></label>
            <label className="field"><span>材料类型</span><select value={materialType} onChange={(event) => setMaterialType(event.target.value)}><option value="">全部类型</option>{materialTypes.map((type) => <option key={type} value={type}>{type}</option>)}</select></label>
          </div>{!courses.length && <p className="inline-note">暂未获取到课程，仍可提交问题进行校验。</p>}</section>
          <section className="card editor-card"><div className="card-title-row"><div><span className="step-number">02</span><h2>输入内容</h2></div><span className="muted">必填</span></div>
            <label className="field field-wide"><span>问题</span><textarea value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="例如：什么是奈奎斯特采样定理？" rows={4} /></label>
            <label className="field field-wide"><span>答案</span><textarea value={answer} onChange={(event) => setAnswer(event.target.value)} placeholder="粘贴或输入待校验的答案…" rows={8} /></label>
            <div className="form-actions"><span className="muted">校验将基于所选课程资料执行</span><button className="button button-primary" type="submit" disabled={submitting}>{submitting ? <><span className="spinner spinner-light" /> 校验中…</> : '开始校验'}</button></div>
          </section>
        </form>
      )}
      <section className="results-section"><div className="section-heading"><div><p className="eyebrow">VALIDATION RESULT</p><h2>校验结果</h2></div>{result && <StatusBadge status={statusForChecks(checks)} />}</div>
        {!result && !submitting && <EmptyState title="等待提交校验" description="提交问题和答案后，这里会展示逐项检查、证据与风险提示。" />}
        {submitting && <div className="loading-panel"><span className="spinner" /> 正在分析答案并检索参考资料…</div>}
        {result && <>
          <div className="score-line"><span>整体置信度 · {result.confidence.level}</span><strong>{confidenceText(result.confidence.score)}</strong></div>
          {result.confidence.reasons.length > 0 && <div className="result-summary">置信度依据：{result.confidence.reasons.join('；')}</div>}
          {!checks.length ? <EmptyState title="后端未返回逐项检查" description="当前响应没有检查项，请查看下方参考材料。" /> : <div className="checks-list">{checks.map((check, index) => <article className="check-card" key={`${check.category}-${index}`}><div className="check-header"><div><StatusBadge status={check.status} /><h3>{check.category}</h3></div><span className="confidence">{check.status}</span></div><p className="check-message">{check.message}</p>{check.claim && <div className="evidence"><strong>待核对主张</strong><p>{check.claim}</p></div>}{check.evidence_ids.length > 0 && <div className="source-line">关联证据：{check.evidence_ids.join('、')}</div>}</article>)}</div>}
          {result.risk_flags.length > 0 && <div className="overall-risks"><strong>风险提示</strong>{result.risk_flags.map((risk) => <span key={risk}>{risk}</span>)}</div>}
        </>}
      </section>
      {(searchError || searchResults.length > 0) && <section className="card sources-card"><div className="section-heading"><div><p className="eyebrow">REFERENCE MATERIALS</p><h2>参考材料与证据片段</h2></div><span className="muted">{searchResults.length} 条</span></div>{searchError && <div className="alert alert-warning" role="status">{searchError}</div>}{searchResults.map((evidence) => <article className="material-item" key={`${evidence.material_id}-${evidence.chunk_id ?? 'material'}`}><div className="material-meta"><strong>{evidence.title}</strong><span>{evidence.material_type}</span></div><p>{evidence.snippet}</p><small>来源：{evidence.course}{evidence.topic ? ` · ${evidence.topic}` : ''} · {evidence.source_anchor}{evidence.page_number === null ? '' : ` · 第 ${evidence.page_number} 页`} · 相关度 {confidenceText(evidence.score)}{evidence.verified ? ' · 已核验' : ''}</small></article>)}</section>}
    </div>
  )
}
