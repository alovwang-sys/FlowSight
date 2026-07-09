import { CodeMap } from './CodeMap';
import { CollectionBadge } from './CollectionBadge';
import type { RuntimeSpan, TraceRecord } from '../types';

interface TraceSidebarProps {
  traces: TraceRecord[];
  selectedTraceId: string;
  spans: RuntimeSpan[];
  selectedSpanId: string | null;
  playheadMs: number;
  search: string;
  onSelectTrace: (traceId: string) => void;
  onSelectSpan: (spanId: string) => void;
}

export function TraceSidebar({
  traces,
  selectedTraceId,
  spans,
  selectedSpanId,
  playheadMs,
  search,
  onSelectTrace,
  onSelectSpan,
}: TraceSidebarProps) {
  const normalizedSearch = search.trim().toLocaleLowerCase();
  const filteredTraces = traces.filter(
    (trace) =>
      !normalizedSearch ||
      `${trace.method} ${trace.route}`.toLocaleLowerCase().includes(normalizedSearch) ||
      trace.spans.some((span) =>
        `${span.name} ${span.file}`.toLocaleLowerCase().includes(normalizedSearch),
      ),
  );

  return (
    <aside className="left-column">
      <section className="panel trace-list-panel" aria-label="最近请求">
        <div className="panel-heading compact">
          <div>
            <span className="eyebrow">最近请求</span>
            <strong>{traces.length} 次本地请求</strong>
          </div>
          <button type="button" className="icon-button" aria-label="刷新请求列表">
            ↻
          </button>
        </div>
        <div className="trace-list">
          {filteredTraces.map((trace) => (
            <button
              key={trace.id}
              className={`trace-row ${trace.id === selectedTraceId ? 'is-selected' : ''}`}
              type="button"
              onClick={() => onSelectTrace(trace.id)}
            >
              <div className="trace-row-main">
                <span className={`method-badge method-${trace.method.toLocaleLowerCase()}`}>
                  {trace.method}
                </span>
                <strong>{trace.route}</strong>
                <CollectionBadge status={trace.collectionStatus} />
              </div>
              <div className="trace-row-meta">
                <span>{trace.statusCode ?? '—'}</span>
                <span>{trace.durationMs} ms</span>
                <span>{trace.when}</span>
                {trace.droppedEventCount > 0 && (
                  <span className="trace-drop">↓ {trace.droppedEventCount}</span>
                )}
              </div>
            </button>
          ))}
          {filteredTraces.length === 0 && (
            <div className="empty-state">没有与“{search}”匹配的请求。</div>
          )}
        </div>
      </section>
      <CodeMap
        spans={spans}
        selectedSpanId={selectedSpanId}
        playheadMs={playheadMs}
        search={search}
        onSelectSpan={onSelectSpan}
      />
    </aside>
  );
}
