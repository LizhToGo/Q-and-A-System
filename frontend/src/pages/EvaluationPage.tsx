import { useEffect, useState } from 'react'
import { api, type CategoryMetric, type Metrics } from '../api'
import { EmptyState } from '../components/EmptyState'
import { MetricCard } from '../components/MetricCard'

interface EvaluationPageProps { onOpenWorkbench: () => void }

function percentage(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${Math.round(value <= 1 ? value * 100 : value)}%`
}

function count(value: number | undefined) {
  return value === undefined || Number.isNaN(value) ? '—' : value.toLocaleString('zh-CN')
}

export function EvaluationPage({ onOpenWorkbench }: EvaluationPageProps) {
  const [metrics, setMetrics] = useState<Metrics | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  useEffect(() => {
    let active = true
    api.metrics().then((data) => { if (active) setMetrics(data) }).catch((reason: unknown) => { if (active) setError(reason instanceof Error ? reason.message : '评估指标加载失败。') }).finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [])

  const categories = metrics ? Object.entries(metrics.category_stats) : []
  return <div className="page-shell">
    <section className="page-heading"><div><h1>评估指标</h1><p className="heading-copy">查看当前课程答案校验集的覆盖情况、可追溯性与冲突概览。</p></div><button className="button button-secondary" onClick={onOpenWorkbench}>返回工作台 →</button></section>
    {error && <div className="alert alert-error" role="alert">{error}</div>}
    {loading && <div className="loading-panel"><span className="spinner" /> 正在获取后端指标…</div>}
    {!loading && !metrics && !error && <EmptyState title="暂无评估指标" description="后端尚未返回 metrics 数据。" />}
    {metrics && <>
      <section className="metric-grid"><MetricCard label="覆盖率" value={percentage(metrics.traceability_rate)} hint="评估样本可追溯比例" tone="blue" /><MetricCard label="测试数量" value={count(metrics.evaluation_case_count)} hint={metrics.evaluation_run ? '已完成评估运行' : '尚未执行评估运行'} tone="green" /><MetricCard label="可追溯性" value={percentage(metrics.traceability_rate)} hint="答案与证据关联程度" tone="purple" /><MetricCard label="冲突准确率" value={percentage(metrics.conflict_accuracy)} hint="冲突识别准确程度" tone="orange" /><MetricCard label="误报率" value={percentage(metrics.false_positive_rate)} hint="冲突识别误报比例" tone="orange" /></section>
      <section className="metric-grid catalog-metrics"><MetricCard label="课程数" value={count(metrics.course_count)} hint="已登记课程" tone="blue" /><MetricCard label="主题数" value={count(metrics.topic_count)} hint="已登记主题" tone="blue" /><MetricCard label="材料数" value={count(metrics.material_count)} hint={`已核验 ${metrics.verified_material_count.toLocaleString('zh-CN')} 条`} tone="green" /></section>
      <section className="card category-card"><div className="section-heading"><div><h2>分类统计</h2></div><span className="muted">{categories.length} 个分类</span></div>{!categories.length ? <EmptyState title="暂无分类数据" description="后端响应中的 category_stats 为空。" /> : <div className="table-wrap"><table><thead><tr><th>分类</th><th>测试数</th><th>正确数</th><th>分类准确率</th><th>TP</th><th>FP</th><th>FN</th><th>TN</th></tr></thead><tbody>{categories.map(([category, stats]: [string, CategoryMetric]) => <tr key={category}><th scope="row">{category}</th><td>{count(stats.total)}</td><td className="positive">{count(stats.correct)}</td><td>{stats.total ? percentage((stats.correct || 0) / stats.total) : '—'}</td><td>{count(stats.tp)}</td><td className="negative">{count(stats.fp)}</td><td className="negative">{count(stats.fn)}</td><td>{count(stats.tn)}</td></tr>)}</tbody></table></div>}</section>
    </>}
  </div>
}
