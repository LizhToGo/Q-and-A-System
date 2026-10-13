import { useEffect, useState } from 'react'
import { API_BASE_URL, api, type EvidenceItem } from '../api'
import { RichText } from './RichText'
import { formatTextbookMarkdown } from './textbookMarkdown'

function imageUrl(path: string) {
  return new URL(path, new URL(API_BASE_URL, window.location.href)).href
}

function SourceImage({ url, label }: { url: string; label: string }) {
  const [failed, setFailed] = useState(false)
  const href = imageUrl(url)
  return <figure className="evidence-media">
    <a href={href} target="_blank" rel="noreferrer">{label} ↗</a>
    {failed ? <p role="status">原图加载失败，可点击上方链接核对原件。</p> : <a href={href} target="_blank" rel="noreferrer" aria-label={`打开${label}`}><img src={href} alt={label} loading="lazy" onError={() => setFailed(true)} /></a>}
  </figure>
}

export function EvidenceView({ evidence }: { evidence: EvidenceItem }) {
  const [full, setFull] = useState<EvidenceItem | null>(null)
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(0)
  const [originalOpen, setOriginalOpen] = useState(false)
  useEffect(() => {
    let current = true
    setError('')
    api.materialEvidence(evidence.material_id).then((value) => {
      if (current) setFull(value)
    }).catch((reason: unknown) => {
      if (current) setError(reason instanceof Error ? reason.message : '完整教材依据加载失败')
    })
    return () => { current = false }
  }, [evidence.material_id, retry])
  const source = full ?? evidence
  return <section className="evidence-row" data-material-id={evidence.material_id}>
    <strong>{source.title}</strong>
    <span>{source.course}{source.topic ? ` · ${source.topic}` : ''} · {source.source_anchor}</span>
    {!full && !error && <p className="muted" role="status">正在加载完整教材依据…</p>}
    {error && <p className="evidence-load-error" role="alert">{error} <button type="button" onClick={() => setRetry((value) => value + 1)}>重试</button></p>}
    {full && <>
      <RichText className={`evidence-textbook${full.content_format === 'text' ? ' evidence-plain-lines' : ''}`} text={formatTextbookMarkdown(full.full_content ?? full.snippet, { structure: full.content_format !== 'markdown' })} normalizeFormulas={false} />
      {(full.media ?? []).map((media) => <SourceImage key={media.id} url={media.url} label={`${media.label} · PDF 第${media.page_number}页`} />)}
      {full.page_image_url && <details className="evidence-original" onToggle={(event) => setOriginalOpen(event.currentTarget.open)}><summary>查看 PDF 第{full.page_number}页原件</summary>{originalOpen && <SourceImage url={full.page_image_url} label={`教材原页 · PDF 第${full.page_number}页`} />}</details>}
    </>}
  </section>
}
