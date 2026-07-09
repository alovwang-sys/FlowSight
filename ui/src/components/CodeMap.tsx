import {
  Background,
  Controls,
  MarkerType,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from '@xyflow/react';
import { useMemo } from 'react';

import { codeNodes } from '../data/mockData';
import type { RuntimeSpan } from '../types';

interface CodeNodeData extends Record<string, unknown> {
  label: string;
  subtitle: string;
  inPath: boolean;
  active: boolean;
  selected: boolean;
  error: boolean;
  sourceChanged: boolean;
  searchMatch: boolean;
}

type CodeFlowNode = Node<CodeNodeData, 'code-node'>;

function CodeNodeCard({ data }: NodeProps<CodeFlowNode>) {
  const classes = [
    'code-node',
    data.inPath ? 'is-in-path' : '',
    data.active ? 'is-active' : '',
    data.selected ? 'is-selected' : '',
    data.error ? 'is-error' : '',
    data.sourceChanged ? 'is-source-changed' : '',
    data.searchMatch ? '' : 'is-search-muted',
  ]
    .filter(Boolean)
    .join(' ');

  return (
    <div className={classes}>
      <span className="code-node-dot" />
      <div>
        <strong>{data.label}</strong>
        <span>{data.subtitle}</span>
      </div>
      {data.sourceChanged && (
        <span className="drift-mark" title="源码已变化">
          !
        </span>
      )}
    </div>
  );
}

const nodeTypes = { 'code-node': CodeNodeCard };

export function runtimeEdgesFromSpans(spans: RuntimeSpan[]): Edge[] {
  const spanById = new Map(spans.map((span) => [span.id, span]));
  const seen = new Set<string>();
  const runtimeEdges: Edge[] = [];
  for (const span of spans) {
    const parent = span.parentId ? spanById.get(span.parentId) : undefined;
    if (!parent || parent.codeNodeId === span.codeNodeId) continue;
    const id = `${parent.codeNodeId}-${span.codeNodeId}`;
    if (seen.has(id)) continue;
    seen.add(id);
    runtimeEdges.push({
      id,
      source: parent.codeNodeId,
      target: span.codeNodeId,
      type: 'smoothstep',
      markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14 },
      className: 'path-edge',
    });
  }
  return runtimeEdges;
}

export function activeCodeNodeIdsAt(spans: RuntimeSpan[], playheadMs: number): Set<string> {
  return new Set(
    spans
      .filter((span) => playheadMs >= span.startMs && playheadMs <= span.endMs)
      .map((span) => span.codeNodeId),
  );
}

interface CodeMapProps {
  spans: RuntimeSpan[];
  selectedSpanId: string | null;
  playheadMs: number;
  search: string;
  onSelectSpan: (spanId: string) => void;
}

export function CodeMap({ spans, selectedSpanId, playheadMs, search, onSelectSpan }: CodeMapProps) {
  const spansByNode = useMemo(() => {
    const map = new Map<string, RuntimeSpan[]>();
    for (const span of spans) {
      const candidates = map.get(span.codeNodeId) ?? [];
      candidates.push(span);
      map.set(span.codeNodeId, candidates);
    }
    return map;
  }, [spans]);

  const selectedSpan = spans.find((span) => span.id === selectedSpanId);
  const selectedNodeKey = selectedSpan?.codeNodeId;
  const normalizedSearch = search.trim().toLocaleLowerCase();
  const activeNodeIds = useMemo(() => activeCodeNodeIdsAt(spans, playheadMs), [playheadMs, spans]);

  const representativeByNode = useMemo(() => {
    const map = new Map<string, RuntimeSpan>();
    for (const [codeNodeId, candidates] of spansByNode) {
      const selected = candidates.find((span) => span.id === selectedSpanId);
      let active: RuntimeSpan | undefined;
      for (const span of candidates) {
        if (playheadMs >= span.startMs && playheadMs <= span.endMs) active = span;
      }
      map.set(codeNodeId, selected ?? active ?? candidates[candidates.length - 1]);
    }
    return map;
  }, [playheadMs, selectedSpanId, spansByNode]);

  const nodes = useMemo<CodeFlowNode[]>(
    () =>
      codeNodes.map((definition) => {
        const span = representativeByNode.get(definition.key);
        return {
          id: definition.key,
          type: 'code-node',
          position: { x: definition.x, y: definition.y },
          draggable: false,
          selectable: true,
          data: {
            label: definition.label,
            subtitle: definition.subtitle,
            inPath: Boolean(span),
            active: activeNodeIds.has(definition.key),
            selected: selectedNodeKey === definition.key,
            error: span?.status === 'error',
            sourceChanged: Boolean(span?.sourceChanged),
            searchMatch:
              !normalizedSearch ||
              `${definition.label} ${definition.subtitle} ${span?.file ?? ''}`
                .toLocaleLowerCase()
                .includes(normalizedSearch),
          },
        };
      }),
    [activeNodeIds, normalizedSearch, representativeByNode, selectedNodeKey],
  );

  const edges = useMemo<Edge[]>(() => runtimeEdgesFromSpans(spans), [spans]);

  return (
    <section className="panel code-map-panel" aria-label="当前请求代码地图">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">代码地图</span>
          <strong>当前请求运行路径</strong>
        </div>
        <span className="legend">
          <i /> RuntimeSpan parent/child
        </span>
      </div>
      <div className="code-map-canvas">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          fitView
          fitViewOptions={{ padding: 0.16 }}
          minZoom={0.52}
          maxZoom={1.3}
          nodesDraggable={false}
          nodesConnectable={false}
          elementsSelectable
          proOptions={{ hideAttribution: true }}
          onNodeClick={(_, node) => {
            const span = representativeByNode.get(node.id);
            if (span) onSelectSpan(span.id);
          }}
        >
          <Background color="#d9d9de" gap={18} size={1} />
          <Controls showInteractive={false} position="bottom-right" />
        </ReactFlow>
      </div>
    </section>
  );
}
