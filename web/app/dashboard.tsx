"use client";

import { useMemo, useState } from "react";
import type { DashboardSnapshot, FlowDirection } from "./lib/dashboard-data";

type DashboardProps = { snapshot: DashboardSnapshot };

const directionLabels: Record<FlowDirection, string> = {
  inflow: "份额增加",
  outflow: "份额减少",
};

export function Dashboard({ snapshot }: DashboardProps) {
  const [direction, setDirection] = useState<FlowDirection>("inflow");
  const flows = snapshot.etfFlows[direction];
  const maxFlow = useMemo(() => Math.max(...flows.map((flow) => Math.abs(flow.value))), [flows]);

  return (
    <main className="app-shell">
      <header className="topbar">
        <a className="brand" href="#top" aria-label="流向台首页">
          <span className="brand-mark" aria-hidden="true">F</span>
          <span>流向台</span>
        </a>
        <nav className="topnav" aria-label="主导航">
          <a className="active" href="#overview">总览</a>
          <a href="#etf">ETF</a>
          <a href="#connect">互联互通</a>
          <a href="#margin">两融</a>
          <a href="#status">数据状态</a>
        </nav>
        <div className="topbar-meta"><span className="live-dot" aria-hidden="true" /><span>日频快照</span></div>
      </header>

      <div className="dashboard" id="top">
        <section className="brief" id="overview" aria-labelledby="brief-title">
          <div>
            <p className="kicker">资金温度 · {snapshot.asOf}</p>
            <h1 id="brief-title">今天的资金，正在流向哪里？</h1>
            <p className="brief-copy">ETF 份额整体增加，半导体与通信方向居前；南向资金净买入，而沪深融资余额连续回落。</p>
          </div>
          <div className="brief-status" aria-label="数据摘要">
            <span className="status-pill status-official">官方源</span>
            <span className="status-pill status-caution">条件通过</span>
            <span className="timestamp">观测于 {snapshot.observedAt}</span>
          </div>
        </section>

        <section className="metric-grid" aria-label="核心资金指标">
          {snapshot.metrics.map((metric) => (
            <article className={`metric-card tone-${metric.tone}`} key={metric.id}>
              <p className="metric-eyebrow">{metric.eyebrow}</p>
              <div className="metric-value-row"><strong>{metric.value}</strong><span>{metric.unit}</span></div>
              <p className="metric-delta">{metric.delta}</p>
              <p className="metric-note">{metric.note}</p>
            </article>
          ))}
        </section>

        <section className="content-grid">
          <article className="panel flow-panel" id="etf">
            <div className="panel-header">
              <div><p className="section-kicker">ETF 资金雷达</p><h2>份额变化排行</h2></div>
              <div className="segmented" role="group" aria-label="ETF 份额变化方向">
                {(Object.keys(directionLabels) as FlowDirection[]).map((item) => (
                  <button aria-pressed={direction === item} className={direction === item ? "selected" : ""} key={item} onClick={() => setDirection(item)} type="button">
                    {directionLabels[item]}
                  </button>
                ))}
              </div>
            </div>
            <div className="flow-list">
              {flows.map((flow, index) => (
                <div className="flow-row" key={flow.code}>
                  <span className="flow-rank">{String(index + 1).padStart(2, "0")}</span>
                  <div className="flow-name"><strong>{flow.name}</strong><span>{flow.code}</span></div>
                  <div className="flow-bar-track" aria-hidden="true">
                    <span className={direction === "inflow" ? "bar-positive" : "bar-negative"} style={{ width: `${Math.max(12, (Math.abs(flow.value) / maxFlow) * 100)}%` }} />
                  </div>
                  <strong className={direction === "inflow" ? "value-positive" : "value-negative"}>{flow.value > 0 ? "+" : ""}{flow.value.toFixed(2)} 亿份</strong>
                </div>
              ))}
            </div>
            <p className="panel-footnote">口径：上交所 ETF 相邻交易日份额差；尚未叠加净值，不等同于资金净申购额。</p>
          </article>

          <article className="panel connect-panel" id="connect">
            <div className="panel-header compact"><div><p className="section-kicker">互联互通</p><h2>南向有方向，北向看活跃度</h2></div></div>
            <div className="connect-list">
              {snapshot.connect.map((item) => (
                <div className="connect-row" key={item.name}>
                  <div><strong>{item.name}</strong><span>{item.description}</span></div>
                  <p className={`connect-value value-${item.tone}`}><strong>{item.value}</strong><span>{item.unit}</span></p>
                </div>
              ))}
            </div>
            <aside className="disclosure-note"><strong>披露口径</strong><p>北向买入、卖出和净流入当前不再公开，因此这里只展示总成交额。</p></aside>
          </article>
        </section>

        <section className="content-grid lower-grid">
          <article className="panel margin-panel" id="margin">
            <div className="panel-header compact">
              <div><p className="section-kicker">融资融券</p><h2>融资余额三日回落</h2></div>
              <span className="change-badge">−1.53% / 3 日</span>
            </div>
            <div className="margin-bars" aria-label="近三个交易日沪深融资余额">
              {snapshot.marginSeries.map((item, index) => {
                const height = 42 + ((item.balance - 28200) / 1000) * 58;
                return (
                  <div className="margin-point" key={item.date}>
                    <span className="margin-label">{item.label}</span>
                    <div className="margin-track"><span className={index === snapshot.marginSeries.length - 1 ? "latest" : ""} style={{ height: `${height}%` }} /></div>
                    <strong>{item.date}</strong>
                  </div>
                );
              })}
            </div>
            <p className="panel-footnote">单位为人民币；余额回落代表杠杆资金规模收缩，不直接等同看空信号。</p>
          </article>

          <article className="panel status-panel" id="status">
            <div className="panel-header compact">
              <div><p className="section-kicker">数据状态</p><h2>每个数字都有日期和来源</h2></div>
              <span className="quality-score">46 / 48 通过</span>
            </div>
            <div className="status-list">
              {snapshot.dataStatus.map((item) => (
                <div className="status-row" key={item.name}>
                  <span className={`status-indicator ${item.status}`} aria-label={item.status === "ready" ? "正常" : "有限制"} />
                  <strong>{item.name}</strong><span>{item.detail}</span><time>{item.asOf}</time>
                </div>
              ))}
            </div>
            <aside className="warning-note"><strong>深交所 ETF 历史限制</strong><p>官方下载只有无统计日期的当前快照。系统会从上线日起每日留存，不伪造历史数据。</p></aside>
          </article>
        </section>

        <footer>
          <p>{snapshot.mode} · 仅供数据观察，不构成投资建议</p>
          <a href="/api/v1/data-status">查看机器可读数据状态</a>
        </footer>
      </div>
    </main>
  );
}
