"use client";

import { ArrowLeft, ArrowsClockwise, Database, MagnifyingGlass, WarningCircle } from "@phosphor-icons/react";
import { useEffect, useMemo, useState } from "react";
import type {
  EtfStockFlowData,
  StockFlowDetail,
  StockFlowDetailContributor,
  StockSearchItem,
} from "./lib/dashboard-data";

type StockSearchPayload = {
  asOf?: string;
  basis?: StockBasis;
  coverage?: { stockCount?: number; matchedEtfCount?: number };
  items?: StockSearchItem[];
  warnings?: string[];
};

type StockShardPayload = {
  asOf?: string;
  basis?: StockBasis;
  coverage?: { stockCount?: number; matchedEtfCount?: number };
  items?: StockFlowDetail[];
  warnings?: string[];
};

type RadarFallbackPayload = {
  data?: { etfStockFlow?: EtfStockFlowData };
  etfStockFlow?: EtfStockFlowData;
};

type StockBasis = {
  shareDate?: string;
  previousShareDate?: string | null;
  pcfDate?: string | null;
  pcfAlignment?: string;
  calculation?: string;
  source?: string;
  cashSubstitution?: string;
};

type Props = {
  asOf: string;
  releaseId?: string | null;
  venue: string | null;
  code: string | null;
};

const LEGACY_RADAR_WARNING = "legacy_radar_embedded_stock_flow";

function inferEmbeddedListingMarket(code: string, listingMarket?: string): string {
  if (listingMarket?.trim()) return listingMarket.trim();
  if (/^6\d{5}$/.test(code)) return "101";
  if (/^[03]\d{5}$/.test(code)) return "102";
  if (/^[48]\d{5}$/.test(code)) return "104";
  return "UNKNOWN";
}

function embeddedStockSearchItems(flow: EtfStockFlowData): StockSearchItem[] {
  const items = new Map<string, StockSearchItem>();
  const add = (stock: { stockCode: string; stockName: string; listingMarket?: string; listingMarketName?: string }) => {
    const stockCode = String(stock.stockCode ?? "").trim();
    const stockName = String(stock.stockName ?? "").trim();
    if (!stockCode || !stockName) return;
    const listingMarket = inferEmbeddedListingMarket(stockCode, stock.listingMarket);
    const identity = `${listingMarket}:${stockCode}`;
    if (items.has(identity)) return;
    items.set(identity, {
      listingMarket,
      listingMarketName: stock.listingMarketName ?? (listingMarket === "101" ? "上交所" : listingMarket === "102" ? "深交所" : listingMarket === "104" ? "北交所" : "待核验市场"),
      code: stockCode,
      name: stockName,
      shard: "",
      available: false,
    });
  };
  flow.stocks.forEach(add);
  flow.etfs.forEach((etf) => etf.stocks?.forEach(add));
  return [...items.values()];
}

function embeddedStockDetail(
  flow: EtfStockFlowData,
  code: string,
  listingMarket?: string,
): StockFlowDetail | null {
  const matchesMarket = (candidate?: string) => !listingMarket || inferEmbeddedListingMarket(code, candidate) === listingMarket;
  const global = flow.stocks.find((stock) => stock.stockCode === code && matchesMarket(stock.listingMarket));
  const direct = flow.etfs.flatMap((etf) => (etf.stocks ?? [])
    .filter((stock) => stock.stockCode === code && matchesMarket(stock.listingMarket))
    .map((stock) => ({ etf, stock })));
  if (!global && !direct.length) return null;

  const contributors: StockFlowDetailContributor[] = global?.contributors.map((item) => ({
    etfMarket: (item as unknown as { etfMarket?: string }).etfMarket,
    etfCode: item.etfCode,
    etfName: item.etfName,
    shareDelta: item.shareDelta,
    quantity: item.quantity,
    creationUnit: item.creationUnit,
    stockFlowShares: item.stockFlowShares,
    currentPenetratedShares: item.currentPenetratedShares,
    estimated: item.estimated,
  })) ?? direct.map(({ etf, stock }) => ({
    etfMarket: etf.market,
    etfCode: etf.code,
    etfName: etf.name,
    shareDelta: etf.shareDelta,
    quantity: stock.quantity,
    creationUnit: etf.creationUnit,
    stockFlowShares: stock.stockFlowShares,
    currentPenetratedShares: stock.currentPenetratedShares,
    estimated: stock.estimated,
  }));
  const etfCount = new Set(contributors.map((item) => item.etfCode)).size;
  const netShares = global?.netShares ?? contributors.reduce((sum, item) => sum + item.stockFlowShares, 0);
  const inflowShares = global?.inflowShares ?? contributors.reduce((sum, item) => sum + Math.max(0, item.stockFlowShares), 0);
  const outflowShares = global?.outflowShares ?? contributors.reduce((sum, item) => sum + Math.max(0, -item.stockFlowShares), 0);
  const first = global ?? direct[0]?.stock;
  const resolvedMarket = inferEmbeddedListingMarket(code, first?.listingMarket);
  return {
    listingMarket: resolvedMarket,
    listingMarketName: first?.listingMarketName ?? (resolvedMarket === "101" ? "上交所" : resolvedMarket === "102" ? "深交所" : resolvedMarket === "104" ? "北交所" : "待核验市场"),
    stockCode: code,
    stockName: first?.stockName ?? code,
    netShares,
    inflowShares,
    outflowShares,
    etfCount,
    contributorCount: etfCount,
    shownContributorCount: contributors.length,
    contributorsLimited: global ? contributors.length < global.etfCount : false,
    contributors,
    estimated: true,
  };
}

const venueToListingMarket: Record<string, string> = {
  SSE: "101",
  SZSE: "102",
  HKEX: "103",
  BSE: "104",
};

function formatShares(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const absolute = Math.abs(value);
  const sign = value > 0 ? "+" : value < 0 ? "−" : "";
  if (absolute >= 100_000_000) return `${sign}${(absolute / 100_000_000).toFixed(2)}亿股`;
  if (absolute >= 10_000) return `${sign}${(absolute / 10_000).toFixed(2)}万股`;
  return `${sign}${absolute.toFixed(0)}股`;
}

function formatNumber(value: number | null | undefined) {
  return value === null || value === undefined || !Number.isFinite(value)
    ? "—"
    : value.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}

function navigateToStock(item: StockSearchItem, date: string) {
  const url = new URL(window.location.href);
  url.searchParams.set("view", "stock");
  const venue = Object.entries(venueToListingMarket).find(([, market]) => market === item.listingMarket)?.[0];
  if (venue) url.searchParams.set("venue", venue);
  url.searchParams.set("code", item.code);
  url.searchParams.set("date", date);
  window.location.assign(`${url.pathname}${url.search}`);
}

export function StockDetailView({ asOf, releaseId, venue, code }: Props) {
  const [searchItems, setSearchItems] = useState<StockSearchItem[]>([]);
  const [detail, setDetail] = useState<StockFlowDetail | null>(null);
  const [basis, setBasis] = useState<StockBasis | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const exactListingMarket = venue ? venueToListingMarket[venue] : undefined;
  const date = asOf;
  const historicalRequest = typeof window !== "undefined" && Boolean(new URLSearchParams(window.location.search).get("date"));
  const requestKey = `${date}:${venue ?? ""}:${code ?? ""}`;
  const [loadedKey, setLoadedKey] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    const load = async () => {
      if (!code) throw new Error("missing_stock_code");
      let fallbackFlow: EtfStockFlowData | null = null;
      const loadRadarFallback = async (): Promise<EtfStockFlowData | null> => {
        if (fallbackFlow) return fallbackFlow;
        const fallbackUrl = historicalRequest
          ? `/api/v2/dashboard?date=${encodeURIComponent(date)}`
          : "/api/v2/views/radar";
        const fallbackResponse = await fetch(fallbackUrl, {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!fallbackResponse.ok) return null;
        const fallbackReleaseId = fallbackResponse.headers.get("x-flow-release-id");
        if (releaseId && fallbackReleaseId && releaseId !== fallbackReleaseId) throw new Error("release_pending");
        const payload = await fallbackResponse.json() as RadarFallbackPayload;
        fallbackFlow = payload.data?.etfStockFlow ?? payload.etfStockFlow ?? null;
        return fallbackFlow;
      };
      const loadRadarDetailFallback = async (): Promise<boolean> => {
        const flow = await loadRadarFallback();
        if (!flow) return false;
        const fallbackItems = embeddedStockSearchItems(flow);
        const fallbackDetail = embeddedStockDetail(flow, code, exactListingMarket);
        if (!fallbackDetail) return false;
        setSearchItems(fallbackItems);
        setBasis(flow.basis);
        setWarnings([...flow.warnings, LEGACY_RADAR_WARNING]);
        setDetail(fallbackDetail);
        setError(null);
        return true;
      };
      const searchResponse = await fetch(`/api/v2/stock-search?date=${encodeURIComponent(date)}`, {
        cache: "no-store",
        signal: controller.signal,
      });
      if (!searchResponse.ok) {
        if (await loadRadarDetailFallback()) return;
        throw new Error(searchResponse.status === 404 ? "stock_model_not_published_for_date" : `stock_search_${searchResponse.status}`);
      }
      const searchReleaseId = searchResponse.headers.get("x-flow-release-id");
      if (releaseId && searchReleaseId && releaseId !== searchReleaseId) throw new Error("release_pending");
      const searchPayload = await searchResponse.json() as StockSearchPayload;
      const items = Array.isArray(searchPayload.items) ? searchPayload.items : [];
      setSearchItems(items);
      const matches = items.filter((item) => item.code === code && (!exactListingMarket || item.listingMarket === exactListingMarket));
      const match = matches[0];
      if (!match || !match.shard) {
        if (await loadRadarDetailFallback()) return;
        throw new Error("stock_not_covered");
      }
      const shardResponse = await fetch(`/api/v2/stocks/${encodeURIComponent(match.shard)}?date=${encodeURIComponent(date)}`, {
        cache: "no-store",
        signal: controller.signal,
      });
      if (!shardResponse.ok) {
        if (await loadRadarDetailFallback()) return;
        throw new Error(shardResponse.status === 404 ? "stock_model_not_published_for_date" : `stock_detail_${shardResponse.status}`);
      }
      const shardReleaseId = shardResponse.headers.get("x-flow-release-id");
      if (searchReleaseId && shardReleaseId && searchReleaseId !== shardReleaseId) throw new Error("release_pending");
      if (releaseId && shardReleaseId && releaseId !== shardReleaseId) throw new Error("release_pending");
      const shardPayload = await shardResponse.json() as StockShardPayload;
      const row = (Array.isArray(shardPayload.items) ? shardPayload.items : []).find(
        (item) => item.stockCode === match.code && item.listingMarket === match.listingMarket,
      );
      if (!row) {
        if (await loadRadarDetailFallback()) return;
        throw new Error("stock_not_covered");
      }
      setError(null);
      setBasis(shardPayload.basis ?? searchPayload.basis ?? null);
      setWarnings([...(searchPayload.warnings ?? []), ...(shardPayload.warnings ?? [])]);
      setDetail(row);
    };
    void load()
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        setError(cause instanceof Error ? cause.message : "个股数据加载失败");
    })
      .finally(() => {
        if (!controller.signal.aborted) {
          setLoadedKey(requestKey);
          setLoading(false);
        }
      });
    return () => controller.abort();
  }, [asOf, code, date, exactListingMarket, historicalRequest, releaseId, requestKey]);

  const isLoading = loading || loadedKey !== requestKey;

  const searchResults = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    if (!normalized) return [];
    return searchItems
      .filter((item) => item.code.includes(normalized) || item.name.toLowerCase().includes(normalized))
      .slice(0, 8);
  }, [query, searchItems]);

  const openSearchResult = (item: StockSearchItem) => {
    setQuery("");
    navigateToStock(item, date);
  };

  const contributorTotal = detail?.contributors.reduce((sum, item) => sum + Math.abs(item.stockFlowShares), 0) ?? 0;
  const alignmentLabel = basis?.pcfAlignment === "next_day_previous_trade_date" ? "次日 PCF" : basis?.pcfAlignment === "same_trading_day" ? "同日 PCF" : "日期待核验";
  const venueLabel = detail?.listingMarketName ?? (venue === "SZSE" ? "深交所" : venue === "HKEX" ? "港交所" : venue === "BSE" ? "北交所" : "上交所");

  return (
    <main className="terminal-shell stock-detail-shell">
      <header className="terminal-topbar stock-detail-topbar">
        <div className="brand-block"><span className="brand-mark" aria-hidden="true"><ArrowsClockwise size={20} weight="bold" /></span><div><strong>FLOW</strong><span>啸傲的市场资金流向交易台</span></div></div>
        <div className="stock-detail-actions">
          <label className="global-search stock-search-control"><MagnifyingGlass size={18} /><input aria-label="搜索个股" onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && searchResults[0]) openSearchResult(searchResults[0]); }} placeholder="搜索个股或代码" value={query} /></label>
          <button className="stock-back-button" onClick={() => window.history.back()} type="button"><ArrowLeft size={17} />返回</button>
        </div>
        {query.trim() && <div className="stock-search-results" role="listbox">{searchResults.length ? searchResults.map((item) => <button aria-selected={false} key={`${item.listingMarket}:${item.code}`} onClick={() => openSearchResult(item)} role="option" type="button"><span><strong>{item.name}</strong><small>{item.code} · {item.listingMarketName ?? item.listingMarket}</small></span><em>{item.available ? "个股" : "雷达兼容"}</em></button>) : <span className="stock-search-no-result">没有找到当前 PCF 覆盖个股</span>}</div>}
      </header>

      <section className="stock-detail-heading">
        <div><span className="eyebrow">ETF → 个股 · 申赎篮子等价变化</span><h1>{detail?.stockName ?? (code ?? "个股详情")}</h1><p>{detail?.stockCode ?? code ?? "—"} · {venueLabel} · 数据截至 {date}</p></div>
        <span className="evidence-badge evidence-inferred">{alignmentLabel}</span>
      </section>

      {isLoading && <section className="stock-detail-state" aria-busy="true"><ArrowsClockwise className="is-refreshing" size={25} /><strong>正在读取个股穿透数据</strong><span>只读取已通过 QA 的同一发布版本。</span></section>}
      {!isLoading && error && <section className="stock-detail-state is-error" role="alert"><WarningCircle size={25} /><strong>{error === "stock_not_covered" ? "该个股不在当前 ETF PCF 覆盖范围" : error === "release_pending" ? "新版本仍在发布，暂不拼接不同版本数据" : error === "stock_model_not_published_for_date" ? "该日期尚未发布个股穿透模型" : "暂时无法读取个股数据"}</strong><span>{error === "stock_not_covered" ? "搜索索引只包含当期官方 PCF 已匹配的个股；这不代表证券不存在。" : "请返回上一页或稍后重试。"}</span></section>}

      {!isLoading && !error && detail && (
        <>
          <section className="stock-proof-grid" aria-label="个股数据凭据">
            <div><span>ETF 篮子净变化</span><strong className={detail.netShares >= 0 ? "text-in" : "text-out"}>{formatShares(detail.netShares)}</strong><small>估算股数</small></div>
            <div><span>正向贡献</span><strong className="text-in">{formatShares(detail.inflowShares)}</strong><small>估算股数</small></div>
            <div><span>负向贡献</span><strong className="text-out">−{formatShares(detail.outflowShares).replace(/^[-−+]/, "")}</strong><small>估算股数</small></div>
            <div><span>贡献 ETF</span><strong className="text-cyan">{detail.etfCount}<em>只</em></strong><small>{detail.contributorsLimited ? `已展示 ${detail.shownContributorCount} 只` : "完整贡献列表"}</small></div>
          </section>

          <section className="stock-detail-layout">
            <article className="terminal-panel stock-contributor-panel">
              <div className="panel-heading"><div><span className="eyebrow">当日贡献拆解</span><h2>{detail.stockName} 的贡献 ETF</h2><p>按 ETF 申赎篮子等价变化绝对值排序；不等同二级市场成交资金。</p></div><span className="evidence-badge evidence-inferred">估算股数</span></div>
              <div className="stock-contributor-table-wrap">
                <table className="stock-contributor-table"><thead><tr><th>ETF</th><th>份额变化</th><th>PCF 数量</th><th>个股等价变化</th><th>占展示贡献</th></tr></thead><tbody>{detail.contributors.map((item) => { const ratio = contributorTotal > 0 ? Math.abs(item.stockFlowShares) / contributorTotal : 0; return <tr key={`${item.etfMarket ?? ""}:${item.etfCode}`}><td><strong>{item.etfName}</strong><small>{item.etfCode} · {item.etfMarket ?? "—"}</small></td><td>{formatNumber(item.shareDelta)}</td><td>{formatNumber(item.quantity)}</td><td className={item.stockFlowShares >= 0 ? "text-in" : "text-out"}>{formatShares(item.stockFlowShares)}</td><td><div className="stock-contribution-bar"><i style={{ width: `${Math.min(100, ratio * 100)}%` }} /><span>{(ratio * 100).toFixed(1)}%</span></div></td></tr>; })}</tbody></table>
              </div>
              {detail.contributorsLimited && <p className="method-note"><Database size={15} />贡献 ETF 列表按上限截取；净变化仍按全量匹配 ETF 计算。</p>}
            </article>
            <aside className="terminal-panel stock-method-panel"><Database size={24} /><h3>数据凭据</h3><div className="stock-method-list"><p><b>份额日</b>{basis?.shareDate ?? date}</p><p><b>前一可比日</b>{basis?.previousShareDate ?? "—"}</p><p><b>PCF 发布日</b>{basis?.pcfDate ?? "—"}</p><p><b>对齐方式</b>{alignmentLabel}</p><p><b>计算</b>ETF 份额变化 ÷ 最小申赎单位 × PCF 成分数量</p></div><small>现金替代或缺失字段存在时，仅作为申赎篮子等价估算；不代表实际交收。</small></aside>
          </section>

          <section className="terminal-panel stock-history-empty"><div><span className="eyebrow">历史穿透走势</span><h2>历史节点积累中</h2><p>当前发布版本只展示经过日期门禁的当日穿透值。至少积累两个完整发布日后才绘制折线，不用单日数据制造趋势。</p></div><Database size={28} /></section>
          {warnings.length > 0 && <p className="stock-warning-note"><WarningCircle size={16} />存在来源提示：{Array.from(new Set(warnings)).slice(0, 2).map((warning) => warning === LEGACY_RADAR_WARNING ? "当前使用 ETF 雷达快照兼容数据，完整跨 ETF 个股分片将在下一次完整发布后可用" : warning).join("；")}</p>}
        </>
      )}
      <footer className="terminal-footer"><span><i className="status-light" />个股穿透数据截至 {date}</span><span>估算股数不等同真实成交资金 · 仅供数据观察，不构成投资建议</span></footer>
    </main>
  );
}
