import type { IngestHealth, RuntimeSpan, Snapshot, TraceRecord, Tracepoint } from '../types';

export const apiEndpoints = {
  traces: '/api/v1/traces',
  trace: (requestTraceId: string) => `/api/v1/traces/${requestTraceId}`,
  spans: (requestTraceId: string) => `/api/v1/traces/${requestTraceId}/spans`,
  span: (requestTraceId: string, spanId: string) =>
    `/api/v1/traces/${requestTraceId}/spans/${spanId}`,
  snapshots: (requestTraceId: string, spanId: string) =>
    `/api/v1/traces/${requestTraceId}/spans/${spanId}/snapshots`,
  codeNodes: '/api/v1/code-nodes',
  status: '/api/v1/status',
  tracepoints: '/api/v1/tracepoints',
  tracepoint: (tracepointId: string) => `/api/v1/tracepoints/${tracepointId}`,
} as const;

// Prototype-only contract boundary. No implementation is wired on this branch;
// later phase tasks must validate the real sidecar API before replacing mocks.
export interface FlowSightDataSource {
  listTraces(): Promise<TraceRecord[]>;
  listSpans(requestTraceId: string): Promise<RuntimeSpan[]>;
  listSnapshots(requestTraceId: string, spanId: string): Promise<Snapshot[]>;
  getHealth(): Promise<IngestHealth>;
  listTracepoints(): Promise<Tracepoint[]>;
  createTracepoint(input: {
    codeNodeId: string;
    lineNo: number;
    locationHash: string;
    watchedVars: string[];
  }): Promise<Tracepoint>;
  reconfirmTracepoint(tracepointId: string, input: { locationHash: string }): Promise<Tracepoint>;
  deleteTracepoint(tracepointId: string): Promise<void>;
}
