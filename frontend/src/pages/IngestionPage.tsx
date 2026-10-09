import { useEffect, useState } from 'react'
import { api, type Course, type ExtractedItem, type IngestionJob } from '../api'

export function IngestionPage() {
  const [courses, setCourses] = useState<Course[]>([])
  const [courseId, setCourseId] = useState('1')
  const [jobId, setJobId] = useState('1')
  const [job, setJob] = useState<IngestionJob | null>(null)
  const [selected, setSelected] = useState<ExtractedItem | null>(null)
  const [draft, setDraft] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    api.courses().then(setCourses).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : '课程加载失败。'))
  }, [])

  const loadJob = async () => {
    setBusy(true); setError('')
    try { const loaded = await api.ingestion.job(Number(jobId)); setJob(loaded); setJobId(String(loaded.id)); setSelected(null) }
    catch (reason: unknown) { setError(reason instanceof Error ? reason.message : '任务加载失败。') }
    finally { setBusy(false) }
  }

  const upload = async () => {
    if (!file) return
    setBusy(true); setError(''); setMessage('')
    try { const loaded = await api.ingestion.upload(file, Number(courseId)); setJob(loaded); setJobId(String(loaded.id)); setMessage('教材已解析为待审核候选，尚未发布。') }
    catch (reason: unknown) { setError(reason instanceof Error ? reason.message : '上传失败。') }
    finally { setBusy(false) }
  }

  const review = async (status: 'approved' | 'rejected') => {
    if (!selected) return
    setBusy(true); setError('')
    try { await api.ingestion.review(selected.id, { normalized_text: draft, review_status: status }); await loadJob(); setMessage(status === 'approved' ? '资料已审核通过，可发布。' : '资料已拒绝，不会发布。') }
    catch (reason: unknown) { setError(reason instanceof Error ? reason.message : '审核失败。') }
    finally { setBusy(false) }
  }

  const ocr = async () => {
    if (!selected) return
    setBusy(true); setError('')
    try { const item = await api.ingestion.ocr(selected.id); setSelected(item); setDraft(item.normalized_text); setMessage('单页 OCR 已完成，结果仍需审核。') }
    catch (reason: unknown) { setError(reason instanceof Error ? reason.message : 'OCR 失败。') }
    finally { setBusy(false) }
  }

  const assist = async () => {
    if (!selected) return
    setBusy(true); setError('')
    try { const item = await api.ingestion.assist(selected.id); setSelected(item); setDraft(item.normalized_text); setMessage('模型辅助结果已保存，仍需审核后发布。') }
    catch (reason: unknown) { setError(reason instanceof Error ? reason.message : '模型辅助失败。') }
    finally { setBusy(false) }
  }

  const publish = async () => {
    if (!job) return
    setBusy(true); setError('')
    try { const result = await api.ingestion.publish(job.id, Number(courseId)); setMessage(`发布 ${result.published} 条资料；跳过 ${result.skipped} 条未审核或空资料。`); await loadJob() }
    catch (reason: unknown) { setError(reason instanceof Error ? reason.message : '发布失败。') }
    finally { setBusy(false) }
  }

  return <div className="page-shell">
    <div className="page-heading"><div><p className="eyebrow">KNOWLEDGE INGESTION</p><h1>教材导入与审核</h1><p className="heading-copy">新教材先进入候选区；扫描页需要 OCR/视觉辅助或人工录入，审核后发布。</p></div></div>
    {error && <div className="alert alert-error">{error}</div>}{message && <div className="alert alert-warning">{message}</div>}
    <section className="card editor-card"><div className="field-grid" style={{ gridTemplateColumns: '1fr 1fr 1fr' }}>
      <label className="field">课程<select value={courseId} onChange={(event) => setCourseId(event.target.value)}>{courses.map((course) => <option key={course.id} value={course.id}>{course.name}</option>)}</select></label>
      <label className="field">上传 PDF / DOCX / Markdown<input type="file" accept=".pdf,.docx,.md,.txt" onChange={(event) => setFile(event.target.files?.[0] || null)} /></label>
      <label className="field">已有任务 ID<input value={jobId} onChange={(event) => setJobId(event.target.value)} /></label>
    </div><div className="form-actions"><button className="button button-primary" disabled={busy || !file} onClick={upload}>上传并解析</button><button className="button button-secondary" disabled={busy} onClick={loadJob}>读取任务</button><button className="button button-secondary" disabled={busy || !job} onClick={publish}>发布已审核资料</button></div></section>
    {job && <><div className="result-summary" style={{ marginTop: 20 }}>任务 #{job.id} · 状态 {job.status} · 已处理 {job.processed_pages}/{job.total_pages} 页 · 需要 OCR {Number(job.summary.ocr_pages || 0)} 页 · 候选 {job.items.length} 条</div>
      <div className="workbench-grid"><section className="card editor-card"><h2>解析候选</h2><div style={{ maxHeight: 650, overflow: 'auto', marginTop: 16 }}>{job.items.map((item) => <button key={item.id} onClick={() => { setSelected(item); setDraft(item.normalized_text) }} style={{ width: '100%', textAlign: 'left', border: '1px solid #dfe6f0', borderRadius: 7, padding: 12, marginBottom: 8, background: selected?.id === item.id ? '#eaf1ff' : '#fff' }}>第 {item.page_number} 页 · {item.item_type} · {item.review_status}<br /><small>{item.normalized_text.slice(0, 80) || '无文本，需要 OCR/视觉辅助或人工录入'}</small></button>)}</div></section>
      <section className="card editor-card">{selected ? <><h2>第 {selected.page_number} 页</h2><p className="muted" style={{ marginTop: 10 }}>{selected.source_anchor} · 置信度 {(selected.confidence * 100).toFixed(0)}%</p><label className="field">审核文本<textarea style={{ minHeight: 350 }} value={draft} onChange={(event) => setDraft(event.target.value)} /></label><details style={{ marginTop: 12 }}><summary>原始内容与结构化信息</summary><pre style={{ whiteSpace: 'pre-wrap', fontSize: 11 }}>{JSON.stringify(selected.payload, null, 2)}</pre></details><div className="form-actions"><button className="button button-primary" disabled={busy || !draft.trim()} onClick={() => review('approved')}>审核通过</button><button className="button button-secondary" disabled={busy} onClick={ocr}>执行本地 OCR</button><button className="button button-secondary" disabled={busy} onClick={() => { setBusy(true); setError(''); api.ingestion.assist(selected.id, 'vision').then((item) => { setSelected(item); setDraft(item.normalized_text); setMessage('视觉模型结果已保存，仍需审核后发布。') }).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : '视觉模型辅助失败。')).finally(() => setBusy(false)) }}>模型视觉辅助</button><button className="button button-secondary" disabled={busy} onClick={() => review('rejected')}>拒绝</button></div></> : <p className="muted">从左侧选择一个候选项开始审核。</p>}</section></div></>}
  </div>
}
