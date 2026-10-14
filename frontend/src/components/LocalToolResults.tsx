import { useState } from 'react'
import { API_BASE_URL, type LocalToolResult } from '../api'
import { RichText } from './RichText'

function ToolArtifact({ artifact }: { artifact: LocalToolResult['artifacts'][number] }) {
  const [failed, setFailed] = useState(false)
  const [attempt, setAttempt] = useState(0)
  const href = new URL(artifact.url, new URL(API_BASE_URL, typeof window === 'undefined' ? 'http://localhost/' : window.location.href)).href
  return <figure className="tool-artifact" data-artifact-id={artifact.id}>
    {failed ? <p className="tool-artifact-error" role="status">计算图像加载失败。<button type="button" onClick={() => { setFailed(false); setAttempt((value) => value + 1) }}>重试图像</button></p> : <a href={href} target="_blank" rel="noreferrer" aria-label={`打开计算图像：${artifact.caption}`}><img key={attempt} src={href} alt={artifact.caption} loading="lazy" onError={() => setFailed(true)} /></a>}
    <figcaption><a href={href} target="_blank" rel="noreferrer">{artifact.caption || '查看本地计算图像'} ↗</a></figcaption>
  </figure>
}

export function LocalToolResults({ results }: { results: LocalToolResult[] }) {
  if (!results.length) return null
  return <section className="local-tool-results" aria-label="本地计算结果">
    <div className="tool-section-heading"><strong>本地计算与绘图</strong><span>实际运行结果 · 非教材引用</span></div>
    {results.map((result) => <article key={result.id} className={`local-tool-card tool-${result.status}`} data-tool-id={result.id} data-tool={result.tool}>
      <header><h3>{result.title}</h3><span className="tool-status">{result.status === 'completed' ? '计算完成' : '计算失败'}</span></header>
      <div className="tool-summary" role={result.status === 'error' ? 'alert' : undefined}><RichText text={result.summary} /></div>
      {result.notes.length > 0 && <div className="tool-conditions"><strong>计算条件与说明</strong><ul>{result.notes.map((note, index) => <li key={index}>{note}</li>)}</ul></div>}
      {result.artifacts.map((artifact) => <ToolArtifact key={artifact.id} artifact={artifact} />)}
      <details className="tool-data"><summary>查看输入参数与数值结果</summary><h4>输入参数</h4><pre>{JSON.stringify(result.parameters, null, 2)}</pre><h4>数值结果</h4><pre>{JSON.stringify(result.data, null, 2)}</pre></details>
    </article>)}
  </section>
}
