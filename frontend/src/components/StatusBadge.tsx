import type { ValidationStatus } from '../api'

interface StatusBadgeProps {
  status?: ValidationStatus | string
}

const labels: Record<string, string> = {
  pass: '通过',
  passed: '通过',
  warning: '需关注',
  fail: '未通过',
  failed: '未通过',
  invalid: '无法校验',
  conflict: '发现冲突',
  not_applicable: '不适用',
  error: '检查异常',
  unknown: '未知状态',
}

export function StatusBadge({ status = 'unknown' }: StatusBadgeProps) {
  const normalized = String(status).toLowerCase()
  const tone = ['pass', 'passed'].includes(normalized)
    ? 'success'
    : ['warning', 'unknown', 'invalid'].includes(normalized)
      ? 'warning'
      : ['fail', 'failed', 'conflict', 'error'].includes(normalized)
        ? 'danger'
        : 'neutral'
  return <span className={`status-badge status-${tone}`}>{labels[normalized] ?? status}</span>
}
