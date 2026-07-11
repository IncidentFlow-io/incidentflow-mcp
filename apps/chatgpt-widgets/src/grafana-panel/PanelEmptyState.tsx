type PanelEmptyStateProps = {
  title: string;
  message: string;
};

export function PanelEmptyState({ title, message }: PanelEmptyStateProps) {
  return (
    <div className="empty-state">
      <h2>{title}</h2>
      <p>{message}</p>
    </div>
  );
}
