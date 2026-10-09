interface EmptyStateProps {
  title: string
  description?: string
}

export function EmptyState({ title, description }: EmptyStateProps) {
  return (
    <div className="empty-state" role="status">
      <div className="empty-icon" aria-hidden="true">○</div>
      <strong>{title}</strong>
      {description && <p>{description}</p>}
    </div>
  )
}
