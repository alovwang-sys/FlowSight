import { useEffect, useMemo, useState } from 'react';

import { Inspector } from './components/Inspector';
import { Timeline } from './components/Timeline';
import { TopBar } from './components/TopBar';
import { TraceSidebar } from './components/TraceSidebar';
import { codeNodes, initialHealth, initialTracepoints, initialTraces } from './data/mockData';
import type { Tracepoint } from './types';

export default function App() {
  const traces = initialTraces;
  const [tracepoints, setTracepoints] = useState<Tracepoint[]>(initialTracepoints);
  const [selectedTraceId, setSelectedTraceId] = useState(initialTraces[0].id);
  const [selectedSpanId, setSelectedSpanId] = useState<string | null>(null);
  const [playheadMs, setPlayheadMs] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [search, setSearch] = useState('');

  const selectedTrace = traces.find((trace) => trace.id === selectedTraceId) ?? traces[0];
  const selectedSpan = selectedTrace.spans.find((span) => span.id === selectedSpanId) ?? null;
  const selectedCodeNode = selectedSpan
    ? codeNodes.find((node) => node.key === selectedSpan.codeNodeId)
    : undefined;
  const activeTracepointCount = useMemo(
    () => tracepoints.filter((tracepoint) => tracepoint.state === 'enabled').length,
    [tracepoints],
  );

  useEffect(() => {
    if (!playing) return;
    let animationFrame = 0;
    let previousTime = performance.now();
    const visualDurationMs = Math.max(2400, Math.min(5200, selectedTrace.durationMs * 16));
    const scale = selectedTrace.durationMs / visualDurationMs;

    const step = (now: number) => {
      const elapsed = now - previousTime;
      previousTime = now;
      setPlayheadMs((current) => {
        const next = current + elapsed * speed * scale;
        if (next >= selectedTrace.durationMs) {
          setPlaying(false);
          return selectedTrace.durationMs;
        }
        return next;
      });
      animationFrame = requestAnimationFrame(step);
    };

    animationFrame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(animationFrame);
  }, [playing, selectedTrace.durationMs, speed]);

  const selectTrace = (traceId: string) => {
    setSelectedTraceId(traceId);
    setSelectedSpanId(null);
    setPlayheadMs(0);
    setPlaying(false);
  };

  const togglePlay = () => {
    if (selectedTrace.collectionStatus === 'collecting') return;
    if (playing) {
      setPlaying(false);
      return;
    }
    if (playheadMs >= selectedTrace.durationMs) setPlayheadMs(0);
    setPlaying(true);
  };

  const createTracepoint = (spanId: string, lineNo: number, variables: string[]) => {
    const span = selectedTrace.spans.find((candidate) => candidate.id === spanId);
    const codeNode = span ? codeNodes.find((candidate) => candidate.key === span.codeNodeId) : null;
    if (!span || !codeNode || span.tracepointSupport.state !== 'supported') return;
    const tracepoint: Tracepoint = {
      id: `prototype-${span.codeNodeId}-${lineNo}`,
      codeNodeId: span.codeNodeId,
      lineNo,
      locationHash: codeNode.locationHash,
      watchedVars: variables,
      hitCount: 0,
      state: 'enabled',
    };
    setTracepoints((current) => [
      ...current.filter(
        (candidate) => candidate.codeNodeId !== span.codeNodeId || candidate.lineNo !== lineNo,
      ),
      tracepoint,
    ]);
  };

  const removeTracepoint = (tracepointId: string) => {
    setTracepoints((current) => current.filter((tracepoint) => tracepoint.id !== tracepointId));
  };

  const reconfirmTracepoint = (tracepointId: string, locationHash: string) => {
    setTracepoints((current) =>
      current.map((tracepoint) =>
        tracepoint.id === tracepointId
          ? { ...tracepoint, locationHash, state: 'enabled' }
          : tracepoint,
      ),
    );
  };

  return (
    <div className="app-shell">
      <TopBar
        activeTracepointCount={activeTracepointCount}
        health={initialHealth}
        search={search}
        onSearchChange={setSearch}
      />
      <div className="workspace">
        <TraceSidebar
          traces={traces}
          selectedTraceId={selectedTrace.id}
          spans={selectedTrace.spans}
          selectedSpanId={selectedSpanId}
          playheadMs={playheadMs}
          search={search}
          onSelectTrace={selectTrace}
          onSelectSpan={setSelectedSpanId}
        />
        <Timeline
          key={selectedTrace.id}
          trace={selectedTrace}
          selectedSpanId={selectedSpanId}
          playheadMs={playheadMs}
          playing={playing}
          speed={speed}
          onTogglePlay={togglePlay}
          onSeek={(value) => {
            setPlaying(false);
            setPlayheadMs(value);
          }}
          onSpeedChange={setSpeed}
          onSelectSpan={setSelectedSpanId}
        />
        <Inspector
          key={`${selectedTrace.id}:${selectedSpanId ?? 'trace'}`}
          trace={selectedTrace}
          tracepoints={tracepoints}
          span={selectedSpan}
          currentLocationHash={selectedCodeNode?.locationHash ?? null}
          onSelectSpan={setSelectedSpanId}
          onCreateTracepoint={createTracepoint}
          onRemoveTracepoint={removeTracepoint}
          onReconfirmTracepoint={reconfirmTracepoint}
        />
      </div>
    </div>
  );
}
