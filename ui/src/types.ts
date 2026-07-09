export type CollectionStatus = 'collecting' | 'complete' | 'incomplete';
export type SpanStatus = 'ok' | 'error';
export type SpanKind = 'http_server' | 'route_handler' | 'function' | 'db_query' | 'http_client';

export type SafeValue =
  string | number | boolean | null | SafeValue[] | { [key: string]: SafeValue };

export interface TracepointSupported {
  state: 'supported';
  executableLines: number[];
  variables: string[];
}

export interface TracepointUnsupported {
  state: 'unsupported';
  reason: string;
}

export type TracepointSupport = TracepointSupported | TracepointUnsupported;

export interface RuntimeSpan {
  id: string;
  parentId: string | null;
  codeNodeId: string;
  name: string;
  kind: SpanKind;
  startMs: number;
  endMs: number;
  status: SpanStatus;
  file: string;
  line: number;
  sourceHash: string;
  argsSummary?: Record<string, SafeValue>;
  returnSummary?: SafeValue;
  exceptionSummary?: {
    type: string;
    summary: string;
  };
  sourceChanged?: boolean;
  tracepointSupport: TracepointSupport;
}

export interface Snapshot {
  tracepointId: string;
  spanId: string;
  capturedAt: string;
  locals: Record<string, SafeValue>;
  redactionReport: string[];
  truncated: boolean;
}

export interface Tracepoint {
  id: string;
  codeNodeId: string;
  lineNo: number;
  locationHash: string;
  watchedVars: string[];
  hitCount: number;
  state: 'enabled' | 'stale' | 'disabled';
}

export interface TraceRecord {
  id: string;
  method: 'GET' | 'POST' | 'PATCH' | 'DELETE';
  route: string;
  statusCode: number | null;
  durationMs: number;
  when: string;
  collectionStatus: CollectionStatus;
  collectionNote: string;
  droppedEventCount: number;
  errorSummary?: string;
  spans: RuntimeSpan[];
  snapshots: Snapshot[];
}

export interface IngestHealth {
  sidecar: 'connected' | 'reconnecting' | 'offline';
  storage: 'healthy' | 'degraded';
  sdkQueueDepth: number;
  writerQueueDepth: number;
  droppedEventCount: number;
  unscopedSpanCount: number;
  lastSuccessAt: string;
}

export interface CodeNodeDefinition {
  key: string;
  locationHash: string;
  label: string;
  subtitle: string;
  x: number;
  y: number;
}
