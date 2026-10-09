interface MetricCardProps {
  label: string
  value: string
  hint?: string
  tone?: 'blue' | 'green' | 'orange' | 'purple'
}

export function MetricCard({ label, value, hint, tone = 'blue' }: MetricCardProps) {
  return (
    <article className={`metric-card metric-${tone}`}>
      <span className="metric-label">{label}</span>
      <strong className="metric-value">{value}</strong>
      {hint && <span className="metric-hint">{hint}</span>}
    </article>
  )
}
