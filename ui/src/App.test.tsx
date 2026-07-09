import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import App from './App';
import { activeCodeNodeIdsAt, runtimeEdgesFromSpans } from './components/CodeMap';
import { Inspector } from './components/Inspector';
import { codeNodes, initialTracepoints, initialTraces } from './data/mockData';

describe('FlowSight v1 workspace', () => {
  it('derives code-map edges from RuntimeSpan parent/child identity', () => {
    const edges = runtimeEdgesFromSpans(initialTraces[0].spans);

    expect(edges.map((edge) => edge.id)).toEqual([
      'http-entry-route',
      'route-order',
      'order-inventory',
      'inventory-inventory-db',
      'order-pricing',
      'order-payment',
      'payment-stripe',
      'order-orders-db',
    ]);
  });

  it('keeps a repeated code node active even when another invocation is selected', () => {
    const spans = initialTraces[0].spans;
    const order = spans.find((span) => span.codeNodeId === 'order');
    expect(order).toBeDefined();
    const repeatedOrder = { ...order!, id: 's3-repeat', startMs: 181, endMs: 186 };

    expect(activeCodeNodeIdsAt([...spans, repeatedOrder], 184).has('order')).toBe(true);
  });

  it('shows honest collection and ingest health states', async () => {
    const user = userEvent.setup();
    render(<App />);

    expect(screen.getByText('原型 · 演示数据')).toBeInTheDocument();
    expect(screen.getByText('当前请求运行路径')).toBeInTheDocument();
    expect(screen.getByText('RuntimeSpan parent/child')).toBeInTheDocument();
    expect(screen.queryByText(/静态上下文/)).not.toBeInTheDocument();
    expect(screen.getAllByText('完整').length).toBeGreaterThan(0);
    expect(screen.getByText('采集中')).toBeInTheDocument();
    expect(screen.getByText('不完整')).toBeInTheDocument();

    const incompleteTrace = screen.getByText('9 分钟前').closest('button');
    expect(incompleteTrace).not.toBeNull();
    await user.click(incompleteTrace!);

    expect(screen.getByText('本次回放不完整')).toBeInTheDocument();
    expect(screen.getByText('3 个事件丢失')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /Sidecar 已连接/ }));
    expect(screen.getByText(/相关 trace 会标记为 incomplete/)).toBeInTheDocument();
  });

  it('creates a named-variable tracepoint with before-line semantics and no expression field', async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.click(
      screen.getByRole('button', { name: '查看 PricingService.calculate_total span' }),
    );
    await user.click(screen.getByRole('button', { name: /添加 Tracepoint/ }));

    expect(screen.getByText(/不支持表达式；语义固定为执行该行之前/)).toBeInTheDocument();
    expect(screen.queryByText('触发条件')).not.toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: '可执行语句行' })).toHaveValue('36');

    const draft = screen.getByRole('group', { name: '观察变量' });
    expect(within(draft).getByRole('checkbox', { name: 'items' })).toBeChecked();
    await user.click(screen.getByRole('button', { name: '启用 Tracepoint' }));

    expect(screen.getByText('Tracepoint 已启用')).toBeInTheDocument();
    expect(screen.getByText('第 36 行执行之前捕获')).toBeInTheDocument();
    expect(screen.getByText('命中 0 次')).toBeInTheDocument();
    expect(screen.getByText('配置已保存，等待下一次请求命中。')).toBeInTheDocument();
    expect(screen.queryByText(/请求开始后 50/)).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /添加 Tracepoint/ }));
    expect(screen.getByRole('combobox', { name: '可执行语句行' })).toHaveValue('38');
    await user.click(screen.getByRole('button', { name: '启用 Tracepoint' }));
    expect(screen.getAllByText('Tracepoint 已启用')).toHaveLength(2);
    expect(screen.getByText('第 38 行执行之前捕获')).toBeInTheDocument();
  });

  it('explains unsupported async tracepoints without hiding normal spans', async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.click(screen.getByRole('button', { name: '查看 PaymentService.charge span' }));

    expect(screen.getByText('当前函数形态不受支持')).toBeInTheDocument();
    expect(screen.getByText(/尚未证明 async 函数隔离/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /添加 Tracepoint/ })).not.toBeInTheDocument();
  });

  it('marks source drift and requires stale tracepoint reconfirmation', async () => {
    const user = userEvent.setup();
    render(<App />);

    const driftTrace = screen.getByText('14 分钟前').closest('button');
    expect(driftTrace).not.toBeNull();
    await user.click(driftTrace!);
    await user.click(screen.getByRole('button', { name: '查看 OrderService.create_order span' }));

    expect(screen.getAllByText('源码已变化').length).toBeGreaterThan(0);
    expect(screen.getByText('绑定已失效')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '重新确认位置' }));
    expect(screen.getByText('Tracepoint 已启用')).toBeInTheDocument();
  });

  it('reconfirms a stale tracepoint with the current CodeNode location hash', async () => {
    const user = userEvent.setup();
    const trace = initialTraces.find((candidate) => candidate.id === 'trace-source-drift')!;
    const span = trace.spans.find((candidate) => candidate.codeNodeId === 'order')!;
    const currentCodeNode = codeNodes.find((candidate) => candidate.key === span.codeNodeId)!;
    const onReconfirmTracepoint = vi.fn();

    render(
      <Inspector
        trace={trace}
        tracepoints={initialTracepoints}
        span={span}
        currentLocationHash={currentCodeNode.locationHash}
        onSelectSpan={vi.fn()}
        onCreateTracepoint={vi.fn()}
        onRemoveTracepoint={vi.fn()}
        onReconfirmTracepoint={onReconfirmTracepoint}
      />,
    );

    await user.click(screen.getByRole('button', { name: '重新确认位置' }));

    expect(onReconfirmTracepoint).toHaveBeenCalledWith(
      'tp-stale-order',
      'sha256:order-service-current-v2',
    );
  });
});
