import { useEffect, useRef, useState } from 'react';

import type { IngestHealth } from '../types';

interface TopBarProps {
  activeTracepointCount: number;
  health: IngestHealth;
  search: string;
  onSearchChange: (value: string) => void;
}

export function TopBar({ activeTracepointCount, health, search, onSearchChange }: TopBarProps) {
  const [healthOpen, setHealthOpen] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);
  const degraded = health.storage === 'degraded' || health.sidecar !== 'connected';

  useEffect(() => {
    const focusSearch = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLocaleLowerCase() === 'k') {
        event.preventDefault();
        searchRef.current?.focus();
      }
    };
    window.addEventListener('keydown', focusSearch);
    return () => window.removeEventListener('keydown', focusSearch);
  }, []);

  return (
    <header className="topbar">
      <div className="brand-mark" aria-hidden="true">
        <span />
      </div>
      <div className="brand-copy">
        <strong>FlowSight</strong>
        <span>FastAPI 本地运行时回放</span>
      </div>
      <span className="demo-pill">原型 · 演示数据</span>

      <label className="search-box">
        <span aria-hidden="true">⌕</span>
        <span className="sr-only">搜索函数、文件或路由</span>
        <input
          ref={searchRef}
          value={search}
          onChange={(event) => onSearchChange(event.target.value)}
          placeholder="搜索函数、文件或路由…"
        />
        <kbd>⌘ K</kbd>
      </label>

      <div className="topbar-actions">
        <div className="tracepoint-count">
          <span className="live-dot" />
          {activeTracepointCount} 个 Tracepoint
        </div>
        <div className="health-menu">
          <button
            className={`health-button ${degraded ? 'is-degraded' : ''}`}
            type="button"
            aria-expanded={healthOpen}
            onClick={() => setHealthOpen((open) => !open)}
          >
            <span className="health-dot" />
            Sidecar {health.sidecar === 'connected' ? '已连接' : '异常'}
            {health.droppedEventCount > 0 && (
              <span className="drop-count">{health.droppedEventCount} dropped</span>
            )}
          </button>
          {healthOpen && (
            <div className="health-popover" role="status">
              <div className="popover-title">采集健康</div>
              <dl>
                <div>
                  <dt>存储</dt>
                  <dd>{health.storage === 'healthy' ? '正常' : '降级'}</dd>
                </div>
                <div>
                  <dt>SDK 队列</dt>
                  <dd>{health.sdkQueueDepth}</dd>
                </div>
                <div>
                  <dt>写入队列</dt>
                  <dd>{health.writerQueueDepth}</dd>
                </div>
                <div>
                  <dt>未归属 spans</dt>
                  <dd>{health.unscopedSpanCount}</dd>
                </div>
                <div>
                  <dt>最后写入</dt>
                  <dd>{health.lastSuccessAt}</dd>
                </div>
              </dl>
              <p>{health.droppedEventCount} 个事件已丢失；相关 trace 会标记为 incomplete。</p>
            </div>
          )}
        </div>
      </div>
    </header>
  );
}
