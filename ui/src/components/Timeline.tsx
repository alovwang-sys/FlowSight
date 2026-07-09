import { useState } from 'react';

import { CollectionBadge } from './CollectionBadge';
import type { RuntimeSpan, SpanKind, TraceRecord } from '../types';

const kindLabels: Record<SpanKind, string> = {
  http_server: 'HTTP',
  route_handler: 'Route',
  function: 'Function',
  db_query: 'DB',
  http_client: 'API',
};

function depthOf(spans: RuntimeSpan[], span: RuntimeSpan) {
  let depth = 0;
  let parentId = span.parentId;
  while (parentId) {
    depth += 1;
    parentId = spans.find((candidate) => candidate.id === parentId)?.parentId ?? null;
  }
  return depth;
}

function isHidden(spans: RuntimeSpan[], span: RuntimeSpan, collapsed: Set<string>) {
  let parentId = span.parentId;
  while (parentId) {
    if (collapsed.has(parentId)) return true;
    parentId = spans.find((candidate) => candidate.id === parentId)?.parentId ?? null;
  }
  return false;
}

interface TimelineProps {
  trace: TraceRecord;
  selectedSpanId: string | null;
  playheadMs: number;
  playing: boolean;
  speed: number;
  onTogglePlay: () => void;
  onSeek: (value: number) => void;
  onSpeedChange: (value: number) => void;
  onSelectSpan: (spanId: string) => void;
}

export function Timeline({
  trace,
  selectedSpanId,
  playheadMs,
  playing,
  speed,
  onTogglePlay,
  onSeek,
  onSpeedChange,
  onSelectSpan,
}: TimelineProps) {
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const canReplay = trace.collectionStatus !== 'collecting';
  const visibleSpans = trace.spans.filter((span) => !isHidden(trace.spans, span, collapsed));

  const toggleCollapsed = (spanId: string) => {
    setCollapsed((current) => {
      const next = new Set(current);
      if (next.has(spanId)) next.delete(spanId);
      else next.add(spanId);
      return next;
    });
  };

  return (
    <main className="panel timeline-panel">
      <div className="timeline-header">
        <div>
          <div className="timeline-title-row">
            <span className={`method-badge method-${trace.method.toLocaleLowerCase()}`}>
              {trace.method}
            </span>
            <h1>{trace.route}</h1>
            <CollectionBadge status={trace.collectionStatus} />
          </div>
          <p>
            {trace.spans.length} 个 spans · {trace.durationMs} ms · HTTP{' '}
            {trace.statusCode ?? '等待中'}
          </p>
        </div>
        <div className="playback-controls">
          <button
            className="play-button"
            type="button"
            onClick={onTogglePlay}
            disabled={!canReplay}
            aria-label={playing ? '暂停回放' : '播放回放'}
          >
            <span className={playing ? 'pause-icon' : 'play-icon'} />
          </button>
          <div className="speed-control" aria-label="回放速度">
            {[0.5, 1, 2, 4].map((value) => (
              <button
                key={value}
                type="button"
                aria-pressed={speed === value}
                onClick={() => onSpeedChange(value)}
              >
                {value}×
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className={`collection-notice is-${trace.collectionStatus}`} role="status">
        <span className="notice-icon" aria-hidden="true">
          {trace.collectionStatus === 'complete'
            ? '✓'
            : trace.collectionStatus === 'collecting'
              ? '···'
              : '!'}
        </span>
        <div>
          <strong>
            {trace.collectionStatus === 'complete' && '本次采集已完成'}
            {trace.collectionStatus === 'collecting' && '请求仍在采集中'}
            {trace.collectionStatus === 'incomplete' && '本次回放不完整'}
          </strong>
          <span>{trace.collectionNote}</span>
        </div>
        {trace.droppedEventCount > 0 && <b>{trace.droppedEventCount} 个事件丢失</b>}
      </div>

      <div className="ruler-block">
        <input
          aria-label="回放时间点"
          type="range"
          min={0}
          max={trace.durationMs}
          step={0.1}
          value={Math.min(playheadMs, trace.durationMs)}
          onChange={(event) => onSeek(Number(event.target.value))}
          disabled={!canReplay}
          style={
            { '--progress': `${(playheadMs / trace.durationMs) * 100}%` } as React.CSSProperties
          }
        />
        <div>
          <span>{playheadMs.toFixed(1)} ms</span>
          <span>{trace.durationMs} ms</span>
        </div>
      </div>

      <div className="timeline-legend">
        {Object.entries(kindLabels).map(([kind, label]) => (
          <span key={kind}>
            <i className={`kind-${kind}`} />
            {label}
          </span>
        ))}
      </div>

      <div className="timeline-list">
        {visibleSpans.map((span) => {
          const depth = depthOf(trace.spans, span);
          const duration = span.endMs - span.startMs;
          const hasChildren = trace.spans.some((candidate) => candidate.parentId === span.id);
          const active = playheadMs >= span.startMs && playheadMs <= span.endMs;
          return (
            <div
              className={`timeline-row ${selectedSpanId === span.id ? 'is-selected' : ''} ${active ? 'is-active' : ''}`}
              key={span.id}
              style={{ '--depth': depth } as React.CSSProperties}
            >
              <div className="span-heading">
                {hasChildren ? (
                  <button
                    className={`collapse-button ${collapsed.has(span.id) ? '' : 'is-open'}`}
                    type="button"
                    onClick={() => toggleCollapsed(span.id)}
                    aria-label={`${collapsed.has(span.id) ? '展开' : '折叠'} ${span.name}`}
                  >
                    ›
                  </button>
                ) : (
                  <span className="collapse-spacer" />
                )}
                <button
                  className="span-select"
                  type="button"
                  onClick={() => onSelectSpan(span.id)}
                  aria-label={`查看 ${span.name} span`}
                >
                  <span
                    className={`kind-dot kind-${span.kind} ${span.status === 'error' ? 'is-error' : ''}`}
                  />
                  <strong>{span.name}</strong>
                  {span.sourceChanged && <span className="source-changed-chip">源码已变化</span>}
                  <span>{duration.toFixed(1)} ms</span>
                </button>
              </div>
              <button
                className="waterfall-track"
                type="button"
                aria-label={`查看 ${span.name} 时间条`}
                onClick={() => onSelectSpan(span.id)}
              >
                <span
                  className={`waterfall-bar kind-${span.kind} ${span.status === 'error' ? 'is-error' : ''}`}
                  style={{
                    left: `${(span.startMs / trace.durationMs) * 100}%`,
                    width: `${Math.max(1.1, (duration / trace.durationMs) * 100)}%`,
                  }}
                />
              </button>
            </div>
          );
        })}
      </div>
    </main>
  );
}
