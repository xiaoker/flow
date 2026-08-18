"use client";

import { ArrowUpRight, Database, Info, MagnifyingGlass, Table } from "@phosphor-icons/react";
import ReactECharts from "echarts-for-react";
import { useEffect, useMemo, useState } from "react";
import type {
  EtfPressureData,
  StockPressureContributor,
  StockPressureHistoryPoint,
  StockPressureRow,
} from "./lib/dashboard-data";
import { searchTextMatches } from "./lib/search-utils";

type Props = {
  data?: EtfPressureData;
  onOpenStock: (row: { stockCode: string; listingMarket: string }) => void;
};

type Filter = "all" | "inflow" | "outflow" | "financing" | "lending";
type Sort = "net" | "gross" | "inflow" | "outflow" | "coverage";

function numberValue(value: number | null | undefined, digits = 0) {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return value.toLocaleString("zh-CN", { maximumFractionDigits: digits });
}

function signedShares(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const sign = value > 0 ? "+" : value < 0 ? "−" : "";
  const absolute = Math.abs(value);
  if (absolute >= 100_000_000) return sign + numberValue(absolute / 100_000_000, 2) + "亿股";
  if (absolute >= 10_000) return sign + numberValue(absolute / 10_000, 2) + "万股";
  return sign + numberValue(absolute, 0) + "股";
}

function listingMarketToVenue(listingMarket: string) {
  return listingMarket === "101"
    ? "SSE"
    : listingMarket === "102"
      ? "SZSE"
      : listingMarket === "103"
        ? "HKEX"
        : listingMarket === "104"
          ? "BSE"
          : null;
}

function financingTrillion(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return (value / 1_000_000_000_000).toLocaleString("zh-CN", { minimumFractionDigits: 4, maximumFractionDigits: 4 });
}

function tone(value: number | null | undefined) {
  return value === null || value === undefined ? "" : value >= 0 ? "text-in" : "text-out";
}

function filterRow(row: StockPressureRow, filter: Filter) {
  if (filter === "inflow") return row.inflowShares > 0;
  if (filter === "outflow") return row.outflowShares > 0;
  if (filter === "financing") return row.marginBalanceYuan != null;
  if (filter === "lending") return row.shortBalanceYuan != null;
  return true;
}

function finiteNumber(value: unknown, fallback = 0) {
  const number = typeof value === "number" ? value : Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function nullableNumber(value: unknown) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function normalizeHistory(value: unknown): StockPressureHistoryPoint[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((point) => {
    if (!point || typeof point !== "object") return [];
    const item = point as Record<string, unknown>;
    const date = typeof item.date === "string" ? item.date : "";
    if (!date) return [];
    return [{
      date,
      netShares: finiteNumber(item.netShares),
      inflowShares: finiteNumber(item.inflowShares),
      outflowShares: finiteNumber(item.outflowShares),
      etfCount: Math.max(0, Math.trunc(finiteNumber(item.etfCount))),
      penetratedShares: nullableNumber(item.penetratedShares),
    }];
  });
}

function rolling20FromHistory(history: StockPressureHistoryPoint[]) {
  if (history.length < 20) return null;
  const window = history.slice(-20);
  return {
    from: window[0].date,
    to: window[window.length - 1].date,
    coverage: window.length,
    netShares: window.reduce((sum, item) => sum + item.netShares, 0),
    inflowShares: window.reduce((sum, item) => sum + item.inflowShares, 0),
    outflowShares: window.reduce((sum, item) => sum + item.outflowShares, 0),
  };
}

function normalizeContributors(value: unknown): StockPressureContributor[] | undefined {
  if (!Array.isArray(value)) return undefined;
  return value.filter((item): item is StockPressureContributor => Boolean(
    item && typeof item === "object" &&
    typeof (item as Record<string, unknown>).etfCode === "string" &&
    typeof (item as Record<string, unknown>).etfName === "string",
  ));
}

function normalizeStockDetail(item: Record<string, unknown>, fallback: StockPressureRow): StockPressureRow {
  const history = normalizeHistory(item.history);
  return {
    ...fallback,
    listingMarket: typeof item.listingMarket === "string" ? item.listingMarket : fallback.listingMarket,
    listingMarketName: typeof item.listingMarketName === "string" ? item.listingMarketName : fallback.listingMarketName,
    stockCode: typeof item.stockCode === "string" ? item.stockCode : fallback.stockCode,
    stockName: typeof item.stockName === "string" ? item.stockName : fallback.stockName,
    netShares: finiteNumber(item.netShares, fallback.netShares),
    inflowShares: finiteNumber(item.inflowShares, fallback.inflowShares),
    outflowShares: finiteNumber(item.outflowShares, fallback.outflowShares),
    penetratedShares: nullableNumber(item.penetratedShares) ?? fallback.penetratedShares,
    etfCount: Math.max(0, Math.trunc(finiteNumber(item.etfCount, fallback.etfCount))),
    historyCoverage: history.length || fallback.historyCoverage,
    history,
    rolling20: rolling20FromHistory(history) ?? fallback.rolling20,
    contributors: normalizeContributors(item.contributors),
    closePriceYuan: nullableNumber(item.closePriceYuan),
    floatShares: nullableNumber(item.floatShares),
    floatMarketCapYuan: nullableNumber(item.floatMarketCapYuan),
    marginBalanceYuan: nullableNumber(item.marginBalanceYuan),
    shortBalanceYuan: nullableNumber(item.shortBalanceYuan),
    priceAsOf: typeof item.priceAsOf === "string" ? item.priceAsOf : fallback.priceAsOf,
    marginAsOf: typeof item.marginAsOf === "string" ? item.marginAsOf : fallback.marginAsOf,
  };
}

function rowSortValue(row: StockPressureRow, sort: Sort) {
  if (sort === "gross") return row.inflowShares + row.outflowShares;
  if (sort === "inflow") return row.inflowShares;
  if (sort === "outflow") return row.outflowShares;
  if (sort === "coverage") return row.historyCoverage;
  return Math.abs(row.netShares);
}

export function EtfPressureView({ data, onOpenStock }: Props) {
  const [filter, setFilter] = useState<Filter>("all");
  const [sort, setSort] = useState<Sort>("net");
  const [query, setQuery] = useState("");
  const [selectedDetail, setSelectedDetail] = useState<StockPressureRow | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [selectedIdentity, setSelectedIdentity] = useState<string | null>(() => {
    if (typeof window === "undefined" || !data?.stocks) return null;
    const params = new URLSearchParams(window.location.search);
    const code = params.get("code");
    const venue = params.get("venue");
    return data.stocks.find((row) => row.stockCode === code && listingMarketToVenue(row.listingMarket) === venue)?.identity ?? null;
  });
  const [page, setPage] = useState(0);
  const pageSize = 50;

  const rows = useMemo(() => data?.stocks ?? [], [data]);
  const filteredRows = useMemo(() => {
    return rows
      .filter((row) => filterRow(row, filter))
      .filter((row) => searchTextMatches(query, row.stockName, row.stockCode, row.listingMarketName))
      .sort((left, right) => rowSortValue(right, sort) - rowSortValue(left, sort) || left.stockCode.localeCompare(right.stockCode));
  }, [filter, query, rows, sort]);
  const pageCount = Math.max(1, Math.ceil(filteredRows.length / pageSize));
  const safePage = Math.min(page, pageCount - 1);
  const visibleRows = filteredRows.slice(safePage * pageSize, (safePage + 1) * pageSize);
  const selected = rows.find((row) => row.identity === selectedIdentity) ?? visibleRows[0] ?? rows[0] ?? null;

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    void (async () => {
      // Defer state transitions until the effect has yielded.  This keeps the
      // selection change cheap and avoids a synchronous cascading render.
      await Promise.resolve();
      if (!active) return;
      if (!data || !selected) {
        setSelectedDetail(null);
        setDetailLoading(false);
        return;
      }
      // Releases created before the compact index already contain the details;
      // keep them usable while the new shard-backed releases roll out.
      if (selected.history && selected.history.length > 0 && selected.contributors) {
        setSelectedDetail(selected);
        setDetailLoading(false);
        setDetailError(null);
        return;
      }
      if (!selected.shard) {
        setSelectedDetail(null);
        setDetailLoading(false);
        setDetailError("当前发布版本缺少个股详情分片");
        return;
      }
      setSelectedDetail(null);
      setDetailLoading(true);
      setDetailError(null);
      try {
        const response = await fetch(`/api/v2/stocks/${encodeURIComponent(selected.shard)}?date=${encodeURIComponent(data.asOf)}`, {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) throw new Error(`stock_shard_${response.status}`);
        const payload = await response.json() as { items?: unknown };
        const items = Array.isArray(payload.items) ? payload.items : [];
        const item = items.find((value) => {
          if (!value || typeof value !== "object") return false;
          const candidate = value as Record<string, unknown>;
          return `${candidate.listingMarket ?? ""}:${candidate.stockCode ?? ""}` === selected.identity;
        });
        if (!item || typeof item !== "object") throw new Error("stock_detail_not_found");
        if (active) setSelectedDetail(normalizeStockDetail(item as Record<string, unknown>, selected));
      } catch (cause: unknown) {
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        if (active) setDetailError(cause instanceof Error ? cause.message : "个股详情读取失败");
      } finally {
        if (active && !controller.signal.aborted) setDetailLoading(false);
      }
    })();
    return () => {
      active = false;
      controller.abort();
    };
  }, [data, selected]);

  const selectRow = (row: StockPressureRow) => {
    setSelectedIdentity(row.identity);
    if (typeof window === "undefined") return;
    const url = new URL(window.location.href);
    url.searchParams.set("view", "pressure");
    url.searchParams.set("code", row.stockCode);
    const venue = listingMarketToVenue(row.listingMarket);
    if (venue) url.searchParams.set("venue", venue);
    else url.searchParams.delete("venue");
    window.history.replaceState({}, "", `${url.pathname}${url.search}`);
  };

  const chartOption = (() => {
    const history = selectedDetail?.history ?? [];
    return {
      animation: false,
      grid: { left: 46, right: 18, top: 24, bottom: 28 },
      tooltip: {
        trigger: "axis",
        valueFormatter: (value: unknown) => signedShares(typeof value === "number" ? value : null),
      },
      xAxis: { type: "category", data: history.map((point) => point.date.slice(5)), axisLabel: { color: "#8e9caf" } },
      yAxis: { type: "value", axisLabel: { color: "#8e9caf", formatter: (value: number) => signedShares(value) }, splitLine: { lineStyle: { color: "#213043", type: "dashed" } } },
      series: [{
        type: "bar",
        data: history.map((point) => ({
          value: point.netShares,
          itemStyle: { color: point.netShares >= 0 ? "#e46166" : "#4fb98a", borderRadius: point.netShares >= 0 ? [4, 4, 0, 0] : [0, 0, 4, 4] },
        })),
        barMaxWidth: 18,
      }],
    };
  })();

  if (!data) {
    return <section className="terminal-panel pressure-empty"><Database size={34} /><h2>ETF β压强尚未进入当前发布版本</h2><p>请先完成一次包含个股穿透读模型的完整发布。旧版本不会在 Worker 请求中临时计算。</p></section>;
  }

  return (
    <section className="pressure-page">
      <div className="pressure-heading">
        <div>
          <span className="eyebrow">ETF → 个股 · 申赎篮子等价</span>
          <h2>ETF β压强</h2>
          <p>截至 {data.asOf} · 只展示可由官方 ETF 份额与 PCF 复算的个股穿透；不等同真实资金流或基金实际持仓。</p>
        </div>
        <div className="pressure-proof"><Info size={17} /><span>{data.coverage.history20Coverage}% 个股具备最近 20 个真实发布节点</span></div>
      </div>

      <div className="pressure-summary" aria-label="ETF压强摘要">
        <article><span>覆盖个股</span><strong className="text-cyan">{numberValue(data.coverage.stockCount)}</strong><small>已识别市场 + 代码</small></article>
        <article><span>关联 ETF</span><strong>{numberValue(data.coverage.matchedEtfCount)}</strong><small>按 ETF 市场 + 代码去重</small></article>
        <article><span>ETF 篮子等价净变化</span><strong className={tone(data.summary.netShares)}>{signedShares(data.summary.netShares)}</strong><small>净值不代表没有正负对冲</small></article>
        <article><span>毛流入 / 毛流出</span><strong className="text-amber">{signedShares(data.summary.inflowShares)} / {signedShares(data.summary.outflowShares)}</strong><small>同一单位：股</small></article>
        <article><span>市场融资余额</span><strong className="text-cyan">{financingTrillion(data.summary.marketFinancingBalanceYuan)}<em>万亿</em></strong><small>官方汇总 · {data.marginAsOf ?? data.asOf} · 不下推到个股</small></article>
      </div>

      <div className="pressure-toolbar">
        <div className="direction-tabs" aria-label="压强方向筛选">
          {(["all", "inflow", "outflow", "financing", "lending"] as Filter[]).map((item) => <button className={filter === item ? "active" : ""} key={item} onClick={() => { setFilter(item); setPage(0); }} type="button">{item === "all" ? "全部" : item === "inflow" ? "ETF净增" : item === "outflow" ? "ETF净减" : item === "financing" ? "融资可比" : "融券可比"}</button>)}
        </div>
        <label className="pressure-search"><MagnifyingGlass size={17} /><input aria-label="搜索个股代码或名称" onChange={(event) => { setQuery(event.target.value); setPage(0); }} placeholder="搜索个股 / 代码" value={query} /></label>
        <label className="pressure-sort">排序<select aria-label="ETF压强排序" onChange={(event) => setSort(event.target.value as Sort)} value={sort}><option value="net">净变化绝对值</option><option value="gross">毛变化</option><option value="inflow">流入</option><option value="outflow">流出</option><option value="coverage">历史覆盖</option></select></label>
      </div>

      <div className="pressure-layout">
        <article className="terminal-panel pressure-table-panel">
          <div className="pressure-table-heading"><div><span className="eyebrow">全市场个股</span><h3>ETF 穿透变化</h3></div><span>{filteredRows.length} 条结果 · 第 {safePage + 1}/{pageCount} 页</span></div>
          <div className="pressure-table-wrap">
            <table className="pressure-table"><thead><tr><th>#</th><th>名称</th><th>代码</th><th>穿透持仓等价</th><th>ETF净增/减</th><th>毛流入</th><th>毛流出</th><th>20日覆盖</th><th>两融</th></tr></thead><tbody>
              {visibleRows.map((row, index) => <tr className={selected?.identity === row.identity ? "selected" : ""} key={row.identity} onClick={() => selectRow(row)}>
                <td>{safePage * pageSize + index + 1}</td><td><strong>{row.stockName}</strong><small>{row.listingMarketName}</small></td><td className="mono">{row.stockCode}</td><td>{signedShares(row.penetratedShares)}</td><td className={tone(row.netShares)}>{signedShares(row.netShares)}</td><td className="text-in">{signedShares(row.inflowShares)}</td><td className="text-out">{signedShares(row.outflowShares)}</td><td>{row.rolling20 ? "20/20" : `${Math.min(row.historyCoverage, 20)}/20`}</td><td>{row.marginBalanceYuan === null && row.shortBalanceYuan === null ? "—" : "可比"}</td>
              </tr>)}
            </tbody></table>
            {!visibleRows.length && <div className="pressure-no-results"><Table size={28} /><strong>没有匹配的个股</strong><span>调整方向、排序或搜索条件。</span></div>}
          </div>
          <div className="table-footer"><span>当前页显示 {visibleRows.length} 条 · 缺失官方价格/两融显示为 —</span><div><button disabled={safePage <= 0} onClick={() => setPage((value) => Math.max(0, value - 1))} type="button">上一页</button><button disabled={safePage >= pageCount - 1} onClick={() => setPage((value) => Math.min(pageCount - 1, value + 1))} type="button">下一页</button></div></div>
        </article>

        <aside className="terminal-panel pressure-detail-panel">
          {selected ? <>
            <div className="pressure-detail-heading"><div><span className="eyebrow">选中个股</span><h3>{selected.stockName} <em>{selected.stockCode}</em></h3><p>{selected.listingMarketName} · 关联 {selected.etfCount} 只 ETF</p></div><button className="link-button" onClick={() => onOpenStock(selected)} type="button">打开个股页<ArrowUpRight size={16} /></button></div>
            {detailLoading && <div className="pressure-detail-loading"><Database size={20} /><span>读取个股历史与 ETF 贡献明细…</span></div>}
            {detailError && <div className="pressure-detail-error"><Info size={17} /><span>{detailError}</span></div>}
            <div className="pressure-detail-kpis"><div><span>当日净变化</span><strong className={tone(selected.netShares)}>{signedShares(selected.netShares)}</strong></div><div><span>20日累计</span><strong className={tone(selectedDetail?.rolling20?.netShares ?? selected.rolling20?.netShares)}>{signedShares(selectedDetail?.rolling20?.netShares ?? selected.rolling20?.netShares)}</strong></div></div>
            <div className="pressure-chart-title"><strong>ETF 穿透历史</strong><span>{selectedDetail?.historyCoverage ?? selected.historyCoverage}/21 个真实节点</span></div>
            {selectedDetail && selectedDetail.history && selectedDetail.history.length >= 2 ? <ReactECharts className="pressure-chart" option={chartOption} notMerge lazyUpdate /> : <div className="pressure-chart-empty"><Database size={24} /><strong>{detailLoading ? "正在读取历史节点" : "历史节点不足或暂不可用"}</strong><span>{detailLoading ? "详情来自同一发布版本的个股分片。" : "至少需要两个发布日；当前只展示已确认数据。"}</span></div>}
            <div className="pressure-factor-list"><div><span>穿透持仓等价</span><strong>{signedShares(selectedDetail?.penetratedShares ?? selected.penetratedShares)}</strong></div><div><span>ETF 净增贡献</span><strong className="text-in">{signedShares(selectedDetail?.inflowShares ?? selected.inflowShares)}</strong></div><div><span>ETF 净减抵消</span><strong className="text-out">−{signedShares(selectedDetail?.outflowShares ?? selected.outflowShares).replace("−", "")}</strong></div><div><span>当前收盘价</span><strong>{(selectedDetail?.closePriceYuan ?? selected.closePriceYuan) == null ? "—" : "¥" + numberValue(selectedDetail?.closePriceYuan ?? selected.closePriceYuan, 4)}</strong></div><div><span>融资 / 融券余额</span><strong>{(selectedDetail?.marginBalanceYuan ?? selected.marginBalanceYuan) == null && (selectedDetail?.shortBalanceYuan ?? selected.shortBalanceYuan) == null ? "—" : numberValue(selectedDetail?.marginBalanceYuan ?? selected.marginBalanceYuan, 2) + " / " + numberValue(selectedDetail?.shortBalanceYuan ?? selected.shortBalanceYuan, 2)}</strong></div></div>
            <div className="pressure-contributors"><header><strong>主要 ETF 贡献</strong><span>{selectedDetail?.contributors?.length ?? 0} 条</span></header>{selectedDetail?.contributors?.length ? selectedDetail.contributors.map((item) => <div key={item.etfMarket + ":" + item.etfCode}><span><b>{item.etfName}</b><small>{item.etfCode}</small></span><strong className={tone(item.stockFlowShares)}>{signedShares(item.stockFlowShares)}</strong></div>) : <p>{detailLoading ? "正在读取同一发布版本的贡献明细。" : "当前发布版本没有附带贡献明细。"}</p>}</div>
            <p className="pressure-disclaimer"><Info size={16} />ETF 申赎篮子穿透是估算等价。现金替代、PCF 日期错位或未披露行情/两融数据不会被补成 0。</p>
          </> : <div className="pressure-chart-empty"><Database size={28} /><strong>当前发布版本没有可用个股</strong><span>等待 ETF 份额与 PCF 同日/明确次日对齐。</span></div>}
        </aside>
      </div>
    </section>
  );
}
