import type { CollectionStatus } from '../types';

const labels: Record<CollectionStatus, string> = {
  collecting: '采集中',
  complete: '完整',
  incomplete: '不完整',
};

export function CollectionBadge({ status }: { status: CollectionStatus }) {
  return <span className={`collection-badge is-${status}`}>{labels[status]}</span>;
}
