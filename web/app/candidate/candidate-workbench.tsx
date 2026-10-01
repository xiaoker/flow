"use client";

import { ArrowClockwise, Database, LockKey, MagnifyingGlass, SignOut, WarningCircle } from "@phosphor-icons/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { CAPABILITY_IDS, ETF_PERIOD_IDS, type ReadModelCapabilityId } from "../lib/read-model-contract";
import {
  candidateErrorMessage,
  CandidateRequestGate,
  readCandidateArtifact,
  readCandidateManifest,
  type CandidateEnvelope,
  type CandidateIdentity,
} from "../lib/candidate-read-client";

type SearchItem = { listingMarket?: string; listingMarketName?: string; code?: string; name?: string; shard?: string; available?: boolean };
type SearchPayload = { asOf?: string; items?: SearchItem[]; coverage?: { stockCount?: number; matchedEtfCount?: number } };
type StockPayload = { asOf?: string; items?: Array<Record<string, unknown>> };
type RadarPayload = {
  asOf?: string;
  data?: { etfTerminal?: { historyCoverage?: Record<string, unknown>; dailySeries?: Array<{ date?: unknown; value?: unknown }>; availablePeriods?: string[] } };
};

const CAPABILITY_LABEL: Record<ReadModelCapabilityId, string> = {
  etf_daily_core: "ETF 日频核心",
  etf_radar: "ETF 雷达",
  rotation: "板块轮动",
  national_team: "观察池",
  connect: "互联互通",
  margin: "融资融券",
  etf_periods: "ETF 周期窗口",
  etf_penetration: "ETF 穿透",
  stock_search: "个股搜索",
  stock_detail: "个股详情",
};
const PERIOD_LABEL: Record<(typeof ETF_PERIOD_IDS)[number], string> = {
  "1d": "最近1日", "3d": "最近3日", "1w": "1周", "2w": "2周", "1m": "1月",
  "3m": "3月", "6m": "6月", ytd: "今年来", "12m": "12月",
};
const PERIOD_CHANGES: Partial<Record<(typeof ETF_PERIOD_IDS)[number], number>> = {
  "1d": 1, "3d": 3, "1w": 5, "2w": 10, "1m": 21, "3m": 63, "6m": 126, "12m": 252,
};
const CAPABILITY_STATUS_LABEL: Record<string, string> = {
  READY: "就绪", PARTIAL: "部分覆盖", MISSING: "缺失", WAITING_SOURCES: "等待来源",
  SOURCE_UNAVAILABLE: "来源不可用", BLOCKED_QUALITY: "质量阻断", UNKNOWN: "未知",
};
const PERIOD_STATUS_LABEL: Record<string, string> = { READY: "完整", PARTIAL: "部分", MISSING: "缺失", UNKNOWN: "未知" };

function identityOf(candidate: CandidateEnvelope): CandidateIdentity {
  return { releaseId: candidate.releaseId, asOf: candidate.asOf, manifestSha256: candidate.manifestSha256, slot: candidate.slot };
}

function requestedDateFromReleaseId(releaseId: string): string {
  return releaseId.split(":")[1] ?? "—";
}

function capabilityFor(candidate: CandidateEnvelope, id: ReadModelCapabilityId): Record<string, unknown> | null {
  const value = candidate.manifest.capabilityContract?.capabilities?.[id];
  return value ? value as unknown as Record<string, unknown> : null;
}

function dataOf(payload: RadarPayload) {
  return payload.data?.etfTerminal;
}

function formatCount(value: unknown): string {
  return Number.isSafeInteger(value) && Number(value) >= 0 ? Number(value).toLocaleString("zh-CN") : "—";
}

function formatDecimal(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString("zh-CN", { maximumFractionDigits: 2 }) : "—";
}

function visibleRadarNodeCount(series: Array<{ date?: unknown }> | undefined): number {
  const dates = new Set<string>();
  for (const point of series ?? []) {
    if (typeof point.date !== "string" || !/^[0-9]{2}-[0-9]{2}$/.test(point.date)) continue;
    const month = Number(point.date.slice(0, 2));
    const day = Number(point.date.slice(3));
    const timestamp = Date.UTC(2000, month - 1, day);
    if (new Date(timestamp).toISOString().slice(5, 10) === point.date) dates.add(point.date);
  }
  return dates.size;
}

export function CandidateWorkbench() {
  const [apiKey, setApiKey] = useState("");
  const [releaseIdInput, setReleaseIdInput] = useState("");
  const [candidate, setCandidate] = useState<CandidateEnvelope | null>(null);
  const [searchItems, setSearchItems] = useState<SearchItem[]>([]);
  const [query, setQuery] = useState("");
  const [selectedStock, setSelectedStock] = useState<SearchItem | null>(null);
  const [stockDetail, setStockDetail] = useState<Record<string, unknown> | null>(null);
  const [radar, setRadar] = useState<RadarPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [artifactLoading, setArtifactLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const candidateGate = useRef(new CandidateRequestGate());
  const searchGate = useRef(new CandidateRequestGate());
  const detailGate = useRef(new CandidateRequestGate());
  const radarGate = useRef(new CandidateRequestGate());
  const candidateRef = useRef<CandidateEnvelope | null>(null);

  const invalidateReads = useCallback(() => {
    candidateGate.current.cancel();
    searchGate.current.cancel();
    detailGate.current.cancel();
    radarGate.current.cancel();
  }, []);

  useEffect(() => () => invalidateReads(), [invalidateReads]);

  const loadCandidate = useCallback(async (requestedReleaseId = releaseIdInput) => {
    const cleanKey = apiKey.trim();
    const cleanReleaseId = requestedReleaseId.trim();
    if (!cleanKey || !cleanReleaseId) {
      setError("请填写后台密钥和精确 releaseId。候选列表不会被扫描或自动枚举。");
      return;
    }
    invalidateReads();
    candidateRef.current = null;
    setCandidate(null);
    setSearchItems([]);
    setSelectedStock(null);
    setStockDetail(null);
    setRadar(null);
    setError(null);
    setLoading(true);
    const request = candidateGate.current.begin();
    try {
      const loaded = await readCandidateManifest(fetch, cleanReleaseId, cleanKey, request.signal);
      if (!request.isCurrent()) return;
      candidateRef.current = loaded;
      setCandidate(loaded);
      setError(null);
    } catch (cause: unknown) {
      if (!request.signal.aborted && request.isCurrent()) setError(candidateErrorMessage(cause));
    } finally {
      if (request.isCurrent()) setLoading(false);
    }
  }, [apiKey, invalidateReads, releaseIdInput]);

  useEffect(() => {
    if (!candidate || candidate.slot !== "etf_penetration" || capabilityFor(candidate, "stock_search")?.status !== "READY") return;
    const gate = searchGate.current;
    const request = gate.begin();
    const identity = identityOf(candidate);
    const descriptor = candidate.manifest.artifacts.stock_search;
    if (!descriptor) return;
    void readCandidateArtifact<SearchPayload>(fetch, identity, "stock_search", descriptor, apiKey.trim(), request.signal)
      .then((payload) => {
        if (!request.isCurrent() || candidateRef.current?.releaseId !== identity.releaseId || candidateRef.current.manifestSha256 !== identity.manifestSha256) return;
        setSearchItems(Array.isArray(payload.items) ? payload.items : []);
      })
      .catch((cause: unknown) => {
        if (!request.signal.aborted && request.isCurrent()) setError(candidateErrorMessage(cause));
      });
    return () => gate.cancel();
  }, [apiKey, candidate]);

  const filteredStocks = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    if (!needle) return searchItems.slice(0, 80);
    return searchItems.filter((item) => `${item.name ?? ""} ${item.code ?? ""} ${item.listingMarketName ?? ""}`.toLocaleLowerCase().includes(needle)).slice(0, 80);
  }, [query, searchItems]);

  const showStock = useCallback((item: SearchItem) => {
    if (!candidate || !item.shard || item.available !== true || capabilityFor(candidate, "stock_detail")?.status !== "READY") return;
    detailGate.current.cancel();
    setSelectedStock(item);
    setStockDetail(null);
    setError(null);
    const descriptor = candidate.manifest.artifacts[item.shard];
    if (!descriptor) {
      setError("该个股分片未在所选候选清单中声明，已拒绝读取。");
      return;
    }
    const request = detailGate.current.begin();
    const identity = identityOf(candidate);
    void readCandidateArtifact<StockPayload>(fetch, identity, item.shard, descriptor, apiKey.trim(), request.signal)
      .then((payload) => {
        if (!request.isCurrent() || candidateRef.current?.releaseId !== identity.releaseId || candidateRef.current.manifestSha256 !== identity.manifestSha256) return;
        const match = (payload.items ?? []).find((row) => row.identity === `${item.listingMarket}:${item.code}` ||
          (row.listingMarket === item.listingMarket && row.stockCode === item.code));
        setStockDetail(match ?? null);
        if (!match) setError("同一候选版本的个股分片中未找到该代码；未回退到生产读取。");
      })
      .catch((cause: unknown) => {
        if (!request.signal.aborted && request.isCurrent()) setError(candidateErrorMessage(cause));
      });
  }, [apiKey, candidate]);

  const loadPeriodNodes = useCallback(() => {
    if (!candidate || !candidate.manifest.artifacts.radar || capabilityFor(candidate, "etf_radar")?.status !== "READY") return;
    radarGate.current.cancel();
    setRadar(null);
    setError(null);
    setArtifactLoading(true);
    const request = radarGate.current.begin();
    const identity = identityOf(candidate);
    const descriptor = candidate.manifest.artifacts.radar;
    if (!descriptor) {
      setArtifactLoading(false);
      return;
    }
    void readCandidateArtifact<RadarPayload>(fetch, identity, "radar", descriptor, apiKey.trim(), request.signal)
      .then((payload) => {
        if (!request.isCurrent() || candidateRef.current?.releaseId !== identity.releaseId || candidateRef.current.manifestSha256 !== identity.manifestSha256) return;
        setRadar(payload);
      })
      .catch((cause: unknown) => {
        if (!request.signal.aborted && request.isCurrent()) setError(candidateErrorMessage(cause));
      })
      .finally(() => {
        if (request.isCurrent()) setArtifactLoading(false);
      });
  }, [apiKey, candidate]);

  const signOut = () => {
    invalidateReads();
    candidateRef.current = null;
    setApiKey("");
    setCandidate(null);
    setSearchItems([]);
    setSelectedStock(null);
    setStockDetail(null);
    setRadar(null);
    setQuery("");
    setError(null);
    setLoading(false);
    setArtifactLoading(false);
  };

  const capabilities = candidate?.manifest.capabilityContract?.capabilities;
  const periodCapability = capabilities?.etf_periods;
  const periodStates = periodCapability?.periods;
  const stockSearchReady = candidate ? capabilityFor(candidate, "stock_search")?.status === "READY" : false;
  const stockDetailReady = candidate ? capabilityFor(candidate, "stock_detail")?.status === "READY" : false;
  const terminal = radar ? dataOf(radar) : undefined;
  const history = terminal?.historyCoverage;
  const tradingDays = history?.tradingDays;
  const radarNodes = visibleRadarNodeCount(terminal?.dailySeries);

  return (
    <main className="candidate-workbench">
      <header className="candidate-header">
        <div><span className="eyebrow">Flow · v0.7 candidate only</span><h1>候选发布预览</h1><p>只读取显式 releaseId 对应的 STAGED 清单与不可变工件；不读取 active pointer，不修改任何生产状态。</p></div>
        {(candidate || apiKey) && <button aria-label="清除页面内存中的候选授权和数据" className="candidate-secondary" onClick={signOut} type="button"><SignOut size={17} />清除会话</button>}
      </header>

      <section className="candidate-panel candidate-access">
        <div className="candidate-access-title"><LockKey size={19} /><div><strong>只读授权</strong><span>后台密钥只由本页内存使用，并通过 HTTPS 发给本域只读接口；应用不写 URL / localStorage / sessionStorage。请只在可信设备输入，并在离开前点击“清除会话”。</span></div></div>
        <label><span>后台密钥</span><input autoComplete="off" onChange={(event) => setApiKey(event.target.value)} placeholder="Bearer token 原文" type="password" value={apiKey} /></label>
        <label><span>STAGED releaseId</span><input autoCapitalize="off" autoComplete="off" onChange={(event) => setReleaseIdInput(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void loadCandidate(); }} placeholder="release:YYYY-MM-DD:16位十六进制" spellCheck={false} value={releaseIdInput} /></label>
        <button className="candidate-primary" disabled={loading || !apiKey.trim() || !releaseIdInput.trim()} onClick={() => void loadCandidate()} type="button">
          {loading ? <ArrowClockwise className="spin" size={17} /> : <Database size={17} />}{loading ? "正在校验候选…" : candidate ? "重新读取此候选" : "读取指定候选"}
        </button>
        <p className="candidate-note">请求仅按 releaseId 做单行精确查询；不会列举候选、不自动刷新、不触发采集或发布。</p>
      </section>

      {error && <div className="candidate-error" role="alert"><WarningCircle size={18} />{error}</div>}

      {candidate && <>
        <section className="candidate-panel candidate-identity">
          <div><span>候选状态</span><strong className="candidate-staged">STAGED · 未激活</strong></div>
          <div><span>请求日期 / 实际 asOf</span><strong>{requestedDateFromReleaseId(candidate.releaseId)} / {candidate.asOf}</strong><small>从 releaseId 取请求日并与 manifest 日期分别显示；合同不一致时拒绝读取</small></div>
          <div><span>发布槽</span><strong>{candidate.slot}</strong></div>
          <div><span>固定版本</span><strong>{candidate.releaseId}</strong><small>manifest SHA-256 · {candidate.manifestSha256}</small></div>
        </section>

        <section className="candidate-panel">
          <div className="candidate-section-heading"><div><span className="eyebrow">Capability contract</span><h2>能力状态与来源日期</h2></div><span>10 项声明 · manifest v{candidate.manifest.schemaVersion}</span></div>
          <div className="candidate-capability-grid">
            {CAPABILITY_IDS.map((id) => {
              const value = capabilities?.[id];
              const status = value?.status ?? "UNKNOWN";
              const sources = Object.entries(value?.sourceDates ?? {}).map(([name, date]) => `${name}: ${date ?? "未证明"}`).join(" · ");
              return <article className="candidate-capability" key={id}>
                <div><strong>{CAPABILITY_LABEL[id]}</strong><span className={`candidate-status status-${String(status).toLowerCase()}`}>{CAPABILITY_STATUS_LABEL[status] ?? status}</span></div>
                <small>能力日期：{value?.asOf ?? "—"} · 工件引用：{value?.artifactRefs.length ?? 0}</small>
                <small>来源：{sources || "未绑定 / 未声明"}</small>
                {value?.reasonCode && <code>{value.reasonCode}</code>}
                {value?.coverage && Object.keys(value.coverage).length > 0 && <small>覆盖：{Object.entries(value.coverage).map(([key, count]) => `${key} ${formatCount(count)}`).join(" · ")}</small>}
              </article>;
            })}
          </div>
        </section>

        <section className="candidate-panel">
          <div className="candidate-section-heading"><div><span className="eyebrow">ETF Periods</span><h2>周期覆盖与缺口</h2></div><span>总历史节点：{formatCount(candidate.manifest.coverage?.etf?.tradingDays)} 个交易日快照</span></div>
          {periodStates ? <div className="candidate-table-scroll"><table className="candidate-period-table"><thead><tr><th>周期</th><th>同版本 Radar 工件节点</th><th>官方变化节点合同</th><th>份额状态 / 可比 ETF 样本</th><th>金额状态 / 可比 ETF 样本</th><th>声明缺口原因</th></tr></thead><tbody>
            {ETF_PERIOD_IDS.map((period) => {
              const row = periodStates[period];
              const shares = row?.shares;
              const amount = row?.estimatedAmount;
              const changes = PERIOD_CHANGES[period];
              const reasons = [shares?.reasonCode, amount?.reasonCode].filter((item, index, list) => item && list.indexOf(item) === index);
              return <tr key={period}>
                <th>{PERIOD_LABEL[period]}</th>
                <td>{radar ? `${formatCount(radarNodes)} 个可识别日期（全工件合计，不按周期分摊）` : "尚未读取同版本 Radar"}</td>
                <td>{changes === undefined ? "年初官方基准 + 本年至今逐日节点" : `${changes} 个变化节点 + 前置基准（${changes + 1} 个官方快照）`}</td>
                <td><span className={`candidate-status status-${shares?.status.toLowerCase() ?? "unknown"}`}>{PERIOD_STATUS_LABEL[shares?.status ?? "UNKNOWN"] ?? "未知"}</span><small>{formatCount(shares?.availableSampleCount)} / {formatCount(shares?.sampleCount)} ETF 样本</small></td>
                <td><span className={`candidate-status status-${amount?.status.toLowerCase() ?? "unknown"}`}>{PERIOD_STATUS_LABEL[amount?.status ?? "UNKNOWN"] ?? "未知"}</span><small>{formatCount(amount?.availableSampleCount)} / {formatCount(amount?.sampleCount)} ETF 样本</small></td>
                <td>{reasons.length ? reasons.map((reason) => <code key={reason}>{reason}</code>) : <span className="candidate-muted">无声明缺口</span>}</td>
              </tr>;
            })}
          </tbody></table></div> : <div className="candidate-empty"><Database size={22} /><strong>该发布槽没有绑定 ETF 周期能力</strong><span>不会从另一个 slot 借数据；UNKNOWN 不等于完整或零缺口。</span></div>}
          <p className="candidate-note">可比 ETF 样本数与日期节点数分开显示。Radar 只包含最近 60 条、且日期字段为 MM-DD 的日变化记录；可见节点数不构成周期完整性证明。manifest 尚未声明逐周期官方日期缺口清单，页面只展示原始 reasonCode，不推算或伪造缺失日期。</p>
          {candidate.slot === "dashboard" && candidate.manifest.artifacts.radar && <div className="candidate-node-action"><button className="candidate-secondary" disabled={artifactLoading} onClick={loadPeriodNodes} type="button">{artifactLoading ? "正在读取精确候选工件…" : "读取同版本 Radar 节点证据"}</button>
            {terminal && <div className="candidate-node-proof"><strong>{formatCount(terminal.dailySeries?.length)} 个日变化节点 · {formatCount(tradingDays)} 个历史交易日快照</strong><span>{String(history?.from ?? "—")} → {String(history?.to ?? candidate.asOf)} · 节点工件日期 {radar?.asOf ?? "—"}</span><span>节点日期来自已校验的同一候选工件；精确缺失官方交易日仍以 070-B 官方日历依赖计划为准。</span></div>}
          </div>}
        </section>

        {candidate.slot === "etf_penetration" && <section className="candidate-panel">
          <div className="candidate-section-heading"><div><span className="eyebrow">Same manifest only</span><h2>候选个股搜索与详情</h2></div><span>{searchItems.length ? `${searchItems.length} 个候选搜索项` : "未读取 / 无就绪搜索能力"}</span></div>
          {stockSearchReady ? <>
            <label className="candidate-stock-search"><MagnifyingGlass size={18} /><input aria-label="候选个股搜索" onChange={(event) => setQuery(event.target.value)} placeholder="搜索候选版本中的个股名称 / 代码" value={query} /></label>
            <div className="candidate-stock-layout"><div className="candidate-stock-results">{filteredStocks.map((item) => <button disabled={!stockDetailReady || item.available !== true} key={`${item.listingMarket}:${item.code}`} onClick={() => showStock(item)} title={!stockDetailReady ? "同一候选版本的个股详情能力未就绪" : undefined} type="button"><span><strong>{item.name}</strong><small>{item.listingMarketName} · {item.code}</small></span><small>{item.available ? item.shard : "不可用"}</small></button>)}{!filteredStocks.length && <div className="candidate-empty"><strong>{searchItems.length ? "没有匹配个股" : "加载同版本搜索工件中…"}</strong><span>最多展示 80 条结果；输入代码或名称缩小范围。</span></div>}</div>
              <article className="candidate-stock-detail"><span className="eyebrow">Stock shard · same release</span>{!stockDetailReady ? <div className="candidate-empty"><Database size={20} /><strong>个股详情能力尚未就绪</strong><span>{String(capabilityFor(candidate, "stock_detail")?.reasonCode ?? "stock_detail_not_ready")} · 搜索结果仍来自该候选版本，不会回退到生产详情。</span></div> : selectedStock ? <><h3>{String(stockDetail?.stockName ?? selectedStock.name)} <small>{String(stockDetail?.stockCode ?? selectedStock.code)}</small></h3><p>{String(stockDetail?.listingMarketName ?? selectedStock.listingMarketName)} · {String(selectedStock.shard)}</p>{stockDetail ? <><div className="candidate-stock-kpis"><span>ETF净变化等价<strong>{formatDecimal(stockDetail.netShares)} 股</strong></span><span>历史节点<strong>{formatCount(stockDetail.historyCoverage)} / 最多21</strong></span><span>关联ETF<strong>{formatCount(stockDetail.etfCount)} 只</strong></span></div><p className="candidate-note">数据来自同一候选 manifest 的 {candidate.asOf} 个股分片，所有值仍为 ETF 篮子穿透估算。</p></> : <div className="candidate-empty">正在读取该候选版本的个股分片…</div>}</> : <div className="candidate-empty"><Database size={20} /><strong>选择个股查看候选详情</strong><span>详情请求固定在 {candidate.releaseId} · {candidate.asOf}。</span></div>}</article>
            </div>
          </> : <div className="candidate-empty"><Database size={22} /><strong>候选搜索 / 详情能力尚未就绪</strong><span>{String(capabilityFor(candidate, "stock_search")?.reasonCode ?? "stock_search_not_ready")} · 不会回退到另一个 release 或生产页面。</span></div>}
        </section>}
      </>}

      {!candidate && !error && <div className="candidate-empty candidate-empty-start"><Database size={28} /><strong>等待显式候选身份</strong><span>输入 releaseId 后才进行一次只读读取。生产 active release 和用户页面完全不受影响。</span></div>}
      <footer className="candidate-footer">候选预览只证明 UI/API 能读取该 STAGED 版本；不代表 QA 通过、canary 通过或已生产发布。所有读取均 no-store，不执行采集、重建、激活或 flag 修改。</footer>
    </main>
  );
}
