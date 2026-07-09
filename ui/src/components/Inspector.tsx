import { useState } from 'react';

import { CollectionBadge } from './CollectionBadge';
import type { RuntimeSpan, Snapshot, Tracepoint, TraceRecord } from '../types';

function SafeJson({ value }: { value: unknown }) {
  if (value === undefined) return <div className="summary-empty">未采集</div>;
  return <pre className="safe-json">{JSON.stringify(value, null, 2)}</pre>;
}

interface TracepointCardProps {
  tracepoint: Tracepoint;
  snapshot: Snapshot | undefined;
  currentLocationHash: string | null;
  onRemove: (tracepointId: string) => void;
  onReconfirm: (tracepointId: string, locationHash: string) => void;
}

function TracepointCard({
  tracepoint,
  snapshot,
  currentLocationHash,
  onRemove,
  onReconfirm,
}: TracepointCardProps) {
  const stateLabel = {
    enabled: 'Tracepoint 已启用',
    stale: '绑定已失效',
    disabled: 'Tracepoint 已禁用',
  }[tracepoint.state];

  return (
    <div className={`tracepoint-card is-${tracepoint.state}`}>
      <div className="tracepoint-card-heading">
        <div>
          <span className="live-dot" />
          <strong>{stateLabel}</strong>
        </div>
        <span>命中 {tracepoint.hitCount} 次</span>
      </div>
      <p>第 {tracepoint.lineNo} 行执行之前捕获</p>
      <div className="variable-chips">
        {tracepoint.watchedVars.map((name) => (
          <span key={name}>{name}</span>
        ))}
      </div>
      {tracepoint.state === 'stale' && (
        <div className="stale-actions">
          <p>源码位置发生漂移。重新确认 current location hash 后才会继续命中。</p>
          <button
            type="button"
            disabled={!currentLocationHash}
            onClick={() => {
              if (currentLocationHash) onReconfirm(tracepoint.id, currentLocationHash);
            }}
          >
            重新确认位置
          </button>
        </div>
      )}
      {snapshot && (
        <div className="snapshot-block">
          <div className="snapshot-meta">
            <span>{snapshot.capturedAt}</span>
            <span>{snapshot.truncated ? '已截断' : '未截断'}</span>
          </div>
          <SafeJson value={snapshot.locals} />
          {snapshot.redactionReport.length > 0 && (
            <p className="redaction-report">已脱敏：{snapshot.redactionReport.join('、')}</p>
          )}
        </div>
      )}
      {!snapshot && tracepoint.state === 'enabled' && (
        <p className="awaiting-snapshot">配置已保存，等待下一次请求命中。</p>
      )}
      <button className="remove-button" type="button" onClick={() => onRemove(tracepoint.id)}>
        移除 Tracepoint
      </button>
    </div>
  );
}

interface InspectorProps {
  trace: TraceRecord;
  tracepoints: Tracepoint[];
  span: RuntimeSpan | null;
  currentLocationHash: string | null;
  onSelectSpan: (spanId: string) => void;
  onCreateTracepoint: (spanId: string, lineNo: number, variables: string[]) => void;
  onRemoveTracepoint: (tracepointId: string) => void;
  onReconfirmTracepoint: (tracepointId: string, locationHash: string) => void;
}

export function Inspector({
  trace,
  tracepoints,
  span,
  currentLocationHash,
  onSelectSpan,
  onCreateTracepoint,
  onRemoveTracepoint,
  onReconfirmTracepoint,
}: InspectorProps) {
  const support = span?.tracepointSupport;
  const [draftOpen, setDraftOpen] = useState(false);
  const [watchedVars, setWatchedVars] = useState<Set<string>>(
    () => new Set(support?.state === 'supported' ? support.variables.slice(0, 2) : []),
  );
  const [lineNo, setLineNo] = useState(
    support?.state === 'supported' ? support.executableLines[0] : 0,
  );

  if (!span) {
    return (
      <aside className="panel inspector-panel">
        <div className="panel-heading">
          <div>
            <span className="eyebrow">Inspector</span>
            <strong>请求概览</strong>
          </div>
        </div>
        <div className="inspector-scroll">
          <div className="trace-overview-title">
            <span className={`method-badge method-${trace.method.toLocaleLowerCase()}`}>
              {trace.method}
            </span>
            <h2>{trace.route}</h2>
          </div>
          <div className="overview-grid">
            <div>
              <span>HTTP 状态</span>
              <strong>{trace.statusCode ?? '等待中'}</strong>
            </div>
            <div>
              <span>耗时</span>
              <strong>{trace.durationMs} ms</strong>
            </div>
            <div>
              <span>采集状态</span>
              <CollectionBadge status={trace.collectionStatus} />
            </div>
            <div>
              <span>事件丢失</span>
              <strong className={trace.droppedEventCount ? 'danger-text' : ''}>
                {trace.droppedEventCount}
              </strong>
            </div>
          </div>
          {trace.errorSummary && (
            <div className="exception-card">
              <strong>安全异常摘要</strong>
              <span>{trace.errorSummary}</span>
            </div>
          )}
          <section className="inspector-section">
            <div className="section-label">运行路径</div>
            <div className="path-list">
              {trace.spans
                .filter((item) => item.kind !== 'http_server')
                .map((item) => (
                  <button key={item.id} type="button" onClick={() => onSelectSpan(item.id)}>
                    <i
                      className={`kind-${item.kind} ${item.status === 'error' ? 'is-error' : ''}`}
                    />
                    <span>{item.name}</span>
                    <b>{(item.endMs - item.startMs).toFixed(1)} ms</b>
                  </button>
                ))}
            </div>
          </section>
          <div className="privacy-note">
            <span aria-hidden="true">⌁</span>
            FlowSight 只展示进入队列前生成的安全摘要，不渲染原始 Python 对象。
          </div>
        </div>
      </aside>
    );
  }

  const nodeTracepoints = tracepoints.filter(
    (candidate) => candidate.codeNodeId === span.codeNodeId,
  );
  const configuredLines = new Set(nodeTracepoints.map((tracepoint) => tracepoint.lineNo));
  const availableLines =
    support?.state === 'supported'
      ? support.executableLines.filter((line) => !configuredLines.has(line))
      : [];
  const canAddTracepoint = nodeTracepoints.length < 5 && availableLines.length > 0;
  const toggleVariable = (name: string) => {
    setWatchedVars((current) => {
      const next = new Set(current);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  return (
    <aside className="panel inspector-panel">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">Inspector</span>
          <strong>Span 详情</strong>
        </div>
        <span className={`span-status is-${span.status}`}>
          {span.status === 'ok' ? '成功' : '异常'}
        </span>
      </div>
      <div className="inspector-scroll">
        <div className="span-title">
          <span
            className={`kind-dot kind-${span.kind} ${span.status === 'error' ? 'is-error' : ''}`}
          />
          <h2>{span.name}</h2>
        </div>
        <div className="source-location">
          {span.file}:{span.line}
        </div>
        {span.sourceChanged && (
          <div className="source-warning" role="status">
            <strong>源码已变化</strong>
            当前文件 hash 与采集时不同，下面的源码位置不代表执行当时内容。
          </div>
        )}
        <div className="overview-grid span-metrics">
          <div>
            <span>耗时</span>
            <strong>{(span.endMs - span.startMs).toFixed(1)} ms</strong>
          </div>
          <div>
            <span>类型</span>
            <strong>{span.kind}</strong>
          </div>
        </div>

        <section className="inspector-section">
          <div className="section-label">安全入参摘要</div>
          <SafeJson value={span.argsSummary} />
        </section>
        <section className="inspector-section">
          <div className="section-label">安全返回摘要</div>
          <SafeJson value={span.returnSummary} />
        </section>
        {span.exceptionSummary && (
          <div className="exception-card">
            <strong>{span.exceptionSummary.type}</strong>
            <span>{span.exceptionSummary.summary}</span>
          </div>
        )}

        <div className="section-divider" />
        <section className="inspector-section tracepoint-section">
          <div className="section-label">Tracepoint</div>
          {support?.state === 'unsupported' && (
            <div className="unsupported-card" role="status">
              <strong>当前函数形态不受支持</strong>
              <span>{support.reason}</span>
            </div>
          )}
          {nodeTracepoints.map((tracepoint) => (
            <TracepointCard
              key={tracepoint.id}
              tracepoint={tracepoint}
              snapshot={trace.snapshots.find(
                (candidate) =>
                  candidate.tracepointId === tracepoint.id && candidate.spanId === span.id,
              )}
              currentLocationHash={currentLocationHash}
              onRemove={onRemoveTracepoint}
              onReconfirm={onReconfirmTracepoint}
            />
          ))}
          {support?.state === 'supported' && canAddTracepoint && !draftOpen && (
            <button
              className="add-tracepoint"
              type="button"
              onClick={() => {
                setLineNo(availableLines[0]);
                setDraftOpen(true);
              }}
            >
              + 添加 Tracepoint
              <span>只捕获指定变量</span>
            </button>
          )}
          {support?.state === 'supported' && canAddTracepoint && draftOpen && (
            <div className="tracepoint-draft">
              <h3>设置 Tracepoint</h3>
              <p>选择可执行语句行与命名变量。不支持表达式；语义固定为执行该行之前。</p>
              <label className="line-select">
                <span>可执行语句行</span>
                <select value={lineNo} onChange={(event) => setLineNo(Number(event.target.value))}>
                  {availableLines.map((line) => (
                    <option key={line} value={line}>
                      第 {line} 行 · 执行前
                    </option>
                  ))}
                </select>
              </label>
              <fieldset>
                <legend>观察变量</legend>
                {support.variables.map((name) => (
                  <label key={name}>
                    <input
                      type="checkbox"
                      checked={watchedVars.has(name)}
                      onChange={() => toggleVariable(name)}
                    />
                    <span>{name}</span>
                  </label>
                ))}
              </fieldset>
              <div className="draft-actions">
                <button type="button" onClick={() => setDraftOpen(false)}>
                  取消
                </button>
                <button
                  className="primary-button"
                  type="button"
                  disabled={watchedVars.size === 0}
                  onClick={() => {
                    onCreateTracepoint(span.id, lineNo, [...watchedVars]);
                    setDraftOpen(false);
                  }}
                >
                  启用 Tracepoint
                </button>
              </div>
            </div>
          )}
        </section>
      </div>
    </aside>
  );
}
