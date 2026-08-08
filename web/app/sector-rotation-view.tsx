"use client";

import { ArrowsLeftRight, CaretLeft, CaretRight, Info, Pulse } from "@phosphor-icons/react";
import { useMemo } from "react";

type RotationRow = {
  name: string;
  groupType?: string;
  value: number;
  componentCount: number;
  comparableCount: number;
  rank?: number;
  unit?: string;
};

type DailyPoint = {
  date: string;
  value: number;
};

type SectorRotationViewProps = {
  asOf: string;
  periodLabel: string;
  metricLabel: string;
  metricUnit: string;
  rows: RotationRow[];
  totalFlow: number;
  crowding: number;
  dailySeries: DailyPoint[];
  onBack: () => void;
  onOpenRadar: () => void;
};

function formatFlow(value: number, digits = 1) {
  return `${value > 0 ? "+" : ""}${value.toFixed(digits)}`;
}

function formatUnit(unit?: string) {
  return unit === "亿元" ? "亿元" : unit;
}

function groupTypeLabel(value?: string) {
  const labels: Record<string, string> = {
    broad: "宽基",
    industry: "行业",
    strategy: "策略",
    theme: "主题",
    cross_border: "跨境",
    bond: "债券",
    commodity: "商品",
    money: "货币",
  };
  return labels[value ?? ""] ?? "板块";
}

function FlowRow({ row, max, direction }: { row: RotationRow; max: number; direction: "in" | "out" }) {
  const width = Math.max(5, Math.round((Math.abs(row.value) / Math.max(max, 0.01)) * 100));
  return (
    <div className={`rotation-flow-row rotation-flow-${direction}`}>
      <div className="rotation-flow-label">
        <span className="rotation-rank">{String(row.rank ?? 0).padStart(2, "0")}</span>
        <span className="rotation-flow-name"><strong>{row.name}</strong><small>{groupTypeLabel(row.groupType)} · {row.comparableCount}/{row.componentCount} 只可比</small></span>
      </div>
      <div className="rotation-flow-bar" aria-hidden="true"><span style={{ width: `${width}%` }} /></div>
      <strong className="rotation-flow-value">{formatFlow(row.value)}<em>{formatUnit(row.unit)}</em></strong>
    </div>
  );
}

export function SectorRotationView({
  asOf,
  periodLabel,
  metricLabel,
  metricUnit,
  rows,
  totalFlow,
  crowding,
  dailySeries,
  onBack,
  onOpenRadar,
}: SectorRotationViewProps) {
  const { outflows, inflows, outflowTotal, inflowTotal } = useMemo(() => {
    const outflowRows = rows
      .filter((row) => row.value < 0)
      .sort((left, right) => left.value - right.value)
      .slice(0, 6)
      .map((row, index) => ({ ...row, rank: index + 1, unit: metricUnit }));
    const inflowRows = rows
      .filter((row) => row.value > 0)
      .sort((left, right) => right.value - left.value)
      .slice(0, 7)
      .map((row, index) => ({ ...row, rank: index + 1, unit: metricUnit }));
    return {
      outflows: outflowRows,
      inflows: inflowRows,
      outflowTotal: outflowRows.reduce((sum, row) => sum + Math.abs(row.value), 0),
      inflowTotal: inflowRows.reduce((sum, row) => sum + row.value, 0),
    };
  }, [metricUnit, rows]);

  const maxOutflow = Math.max(...outflows.map((row) => Math.abs(row.value)), 0.01);
  const maxInflow = Math.max(...inflows.map((row) => row.value), 0.01);
  const topInflow = inflows[0];
  const topOutflow = outflows[0];
  const inflowConcentration = inflowTotal > 0 && topInflow ? Math.round((topInflow.value / inflowTotal) * 100) : 0;
  const outflowConcentration = outflowTotal > 0 && topOutflow ? Math.round((Math.abs(topOutflow.value) / outflowTotal) * 100) : 0;
  const rotationSpeed = useMemo(() => {
    if (dailySeries.length < 3) return null;
    let switches = 0;
    for (let index = 1; index < dailySeries.length; index += 1) {
      const previous = Math.sign(dailySeries[index - 1].value);
      const current = Math.sign(dailySeries[index].value);
      if (previous !== 0 && current !== 0 && previous !== current) switches += 1;
    }
    return Math.round((switches / Math.max(1, dailySeries.length - 1)) * 100);
  }, [dailySeries]);
  const balanceEfficiency = inflowTotal + outflowTotal > 0
    ? Math.round((Math.abs(totalFlow) / (inflowTotal + outflowTotal)) * 100)
    : null;

  return (
    <section className="rotation-page" data-testid="sector-rotation-view">
      <article className="terminal-panel rotation-hero">
        <header className="rotation-page-heading">
          <div className="rotation-page-title">
            <button className="rotation-back" onClick={onBack} type="button"><CaretLeft size={16} weight="bold" />返回总览</button>
            <span className="eyebrow">ETF 份额 → 资金估算 · 版本化板块</span>
            <h2>板块资金轮动</h2>
            <p>资金净流入 = ETF 份额变化 × 前一交易日净值估算 · {periodLabel} · 截至 <b>{asOf}</b></p>
          </div>
          <div className="rotation-page-actions">
            <span className="rotation-date-chip"><Pulse size={16} />{asOf} 官方快照</span>
            <button className="link-button" onClick={onOpenRadar} type="button">查看 ETF 雷达<CaretRight size={16} /></button>
          </div>
        </header>

        <div className="rotation-flow-card">
          <div className="rotation-flow-head">
            <div><span>流出来源</span><strong>流出合计 <b className="text-out">{formatFlow(-outflowTotal)}</b><em>{formatUnit(metricUnit)}</em></strong></div>
            <div className="rotation-flow-head-center"><span>资金桥接</span><small>同一套 ETF 分类口径 · 拥挤度 {crowding}%</small></div>
            <div><span>流入去向</span><strong>流入合计 <b className="text-in">{formatFlow(inflowTotal)}</b><em>{formatUnit(metricUnit)}</em></strong></div>
          </div>

          <div className="rotation-flow-body">
            <div className="rotation-side rotation-side-out" aria-label="流出来源">
              {outflows.length ? outflows.map((row) => <FlowRow direction="out" key={row.name} max={maxOutflow} row={row} />) : <div className="rotation-empty">暂无可比流出板块</div>}
            </div>
            <div className="rotation-net-card">
              <div className="rotation-net-icon"><ArrowsLeftRight size={30} weight="bold" /></div>
              <span>净流入</span>
              <strong className={totalFlow >= 0 ? "text-in" : "text-out"}>{formatFlow(totalFlow, 2)}<em>{formatUnit(metricUnit)}</em></strong>
              <small>{rows.length} 个板块 · {metricLabel}</small>
            </div>
            <div className="rotation-side rotation-side-in" aria-label="流入去向">
              {inflows.length ? inflows.map((row) => <FlowRow direction="in" key={row.name} max={maxInflow} row={row} />) : <div className="rotation-empty">暂无可比流入板块</div>}
            </div>
          </div>
          <p className="rotation-method-note"><Info size={16} />金额是官方份额变化 × 对应交易日官方单位净值的估算，不等同真实申赎现金；缺少官方净值的 ETF 不进入金额汇总。</p>
        </div>
      </article>

      <section className="rotation-insights" aria-label="板块轮动解读">
        <div className="rotation-section-title"><div><span className="eyebrow">结构解读</span><h3>资金从哪里来，又去了哪里</h3></div><span>{rows.length} 个版本化板块 · {periodLabel}</span></div>
        <div className="rotation-insight-grid">
          <article><span className="rotation-insight-index rotation-insight-green">01</span><div><strong>{topOutflow?.name ?? "—"}是主要流出来源</strong><p>单一来源约占流出侧 {outflowConcentration}%；先看资金撤离的行业或资产类型。</p></div></article>
          <article><span className="rotation-insight-index rotation-insight-red">02</span><div><strong>{topInflow?.name ?? "—"}吸收主要资金</strong><p>流入侧集中度约 {inflowConcentration}%；集中度越高，短期主线越清晰，回撤也更敏感。</p></div></article>
          <article><span className="rotation-insight-index rotation-insight-cyan">03</span><div><strong>{totalFlow >= 0 ? "净流入为正，资金仍在寻找承接" : "净流入为负，板块整体偏防御"}</strong><p>流入效率 {balanceEfficiency === null ? "—" : `${balanceEfficiency}%`}；结合 ETF 雷达逐只核对，不把板块汇总当成个股信号。</p></div></article>
        </div>
      </section>

      <section className="rotation-gauge-panel" aria-label="轮动指标">
        <div className="rotation-gauge">
          <div className="rotation-gauge-title"><span>轮动速度</span><strong className="text-cyan">{rotationSpeed === null ? "—" : `${rotationSpeed}%`}</strong></div>
          <div className="rotation-gauge-track"><span style={{ width: `${rotationSpeed ?? 0}%` }} /></div>
          <p>{rotationSpeed === null ? "历史序列不足，暂不计算方向切换率" : rotationSpeed >= 50 ? "净流向方向切换较频繁，主线稳定性偏弱" : "净流向方向切换较少，主线仍有延续"}</p>
        </div>
        <div className="rotation-gauge">
          <div className="rotation-gauge-title"><span>流入集中度</span><strong className="text-amber">{inflowConcentration ? `${inflowConcentration}%` : "—"}</strong></div>
          <div className="rotation-gauge-track rotation-gauge-amber"><span style={{ width: `${inflowConcentration}%` }} /></div>
          <p>{inflowConcentration >= 65 ? "资金明显集中在少数板块，注意拥挤与回撤风险" : "资金分布相对分散，需结合趋势确认是否形成主线"}</p>
        </div>
      </section>

      <p className="rotation-disclaimer"><Info size={15} />数据说明：板块资金为 ETF 申赎方向的估算代理，分类按当前版本化规则合并；仅供资金结构观察，不构成投资建议。</p>
    </section>
  );
}
