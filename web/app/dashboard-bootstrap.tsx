"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Dashboard } from "./dashboard";
import { StockDetailView } from "./stock-detail-view";
import { enrichSnapshotWithMarketHistory } from "./lib/market-history";
import type { DashboardSnapshot } from "./lib/dashboard-data";

type DeskView = "overview" | "rotation" | "radar" | "national" | "connect" | "margin" | "pressure" | "stock";
type ReadModelView = Exclude<DeskView, "overview" | "stock"> | "radar_flow";

type HistoryResponse = {
  dates?: string[];
};

type ReleasePointer = {
  releaseId?: string;
  asOf?: string;
};

type ReadModelEnvelope = {
  schemaVersion?: number;
  asOf?: string;
  observedAt?: string;
  releaseStatus?: string;
  data?: Record<string, unknown>;
};

const REQUEST_TIMEOUT_MS = 15_000;
const REFRESH_ERROR_THRESHOLD = 3;

function requestedView(value: string | null): DeskView | undefined {
  return value === "rotation" || value === "radar" || value === "national" || value === "connect" || value === "margin" || value === "pressure" || value === "stock"
    ? value
    : undefined;
}

export function DashboardBootstrap() {
  const [snapshot, setSnapshot] = useState<DashboardSnapshot | null>(null);
  const [releaseId, setReleaseId] = useState<string | null>(null);
  const [availableDates, setAvailableDates] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [viewLoadError, setViewLoadError] = useState<string | null>(null);
  const [viewLoading, setViewLoading] = useState(false);
  const snapshotRef = useRef<DashboardSnapshot | null>(null);
  const baseSnapshotRef = useRef<DashboardSnapshot | null>(null);
  const releaseIdRef = useRef<string | null>(null);
  const requestRef = useRef<AbortController | null>(null);
  const viewRequestRef = useRef<AbortController | null>(null);
  const viewCacheRef = useRef(new Map<string, Record<string, unknown>>());
  const refreshInFlightRef = useRef(false);
  const refreshFailureCountRef = useRef(0);
  const mountedRef = useRef(true);

  const getRequestedDate = useCallback(() => {
    if (typeof window === "undefined") return null;
    return new URLSearchParams(window.location.search).get("date");
  }, []);

  const viewCacheKey = useCallback((id: string | null, date: string | null, view: ReadModelView) => `${id ?? "date:" + (date ?? "latest")}:${view}`, []);

  const readView = useCallback(async (
    view: ReadModelView,
    base: DashboardSnapshot,
    id: string | null,
    date: string | null,
    signal?: AbortSignal,
  ): Promise<Record<string, unknown>> => {
    const key = viewCacheKey(id, date, view);
    const cached = viewCacheRef.current.get(key);
    if (cached) return cached;
    const response = await fetch(`/api/v2/views/${view}${date ? `?date=${encodeURIComponent(date)}` : ""}`, {
      cache: "no-store",
      signal,
    });
    if (!response.ok) throw new Error(`view_${view}_${response.status}`);
    const viewReleaseId = response.headers.get("x-flow-release-id");
    if (!viewReleaseId || (id && viewReleaseId !== id)) throw new Error("release_pending");
    const envelope = await response.json() as ReadModelEnvelope;
    if (envelope.asOf !== base.asOf) throw new Error("release_pending");
    const data = envelope.data && typeof envelope.data === "object" && !Array.isArray(envelope.data)
      ? envelope.data
      : {};
    viewCacheRef.current.set(key, data);
    while (viewCacheRef.current.size > 32) {
      const oldest = viewCacheRef.current.keys().next().value;
      if (oldest) viewCacheRef.current.delete(oldest);
      else break;
    }
    return data;
  }, [viewCacheKey]);

  const loadSnapshot = useCallback(async (expectedReleaseId?: string) => {
    const date = getRequestedDate();
    const view = typeof window === "undefined"
      ? undefined
      : requestedView(new URLSearchParams(window.location.search).get("view"));
    const snapshotUrl = date
      ? `/api/v2/dashboard?date=${encodeURIComponent(date)}`
      : "/api/v2/dashboard";
    const historyUrl = date
      ? `/api/v2/market-history?asOf=${encodeURIComponent(date)}`
      : "/api/v2/market-history";
    requestRef.current?.abort();
    viewRequestRef.current?.abort();
    viewRequestRef.current = null;
    viewCacheRef.current.clear();
    if (mountedRef.current) setViewLoading(false);
    const controller = new AbortController();
    requestRef.current = controller;
    let timedOut = false;
    const timeoutId = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, REQUEST_TIMEOUT_MS);

    try {
      const [snapshotResponse, datesResponse, marketResponse] = await Promise.all([
        fetch(snapshotUrl, { cache: "no-store", signal: controller.signal }),
        fetch("/api/v2/history/dates", { cache: "no-store", signal: controller.signal }),
        fetch(historyUrl, { cache: "no-store", signal: controller.signal }),
      ]);
      if (!snapshotResponse.ok) {
        throw new Error(snapshotResponse.status === 404 ? "该日期尚未形成完整发布版本" : `dashboard_${snapshotResponse.status}`);
      }
      if (!datesResponse.ok || !marketResponse.ok) throw new Error("history_unavailable");
      const [rawSnapshot, datesPayload, marketPayload] = await Promise.all([
        snapshotResponse.json() as Promise<DashboardSnapshot>,
        datesResponse.json() as Promise<HistoryResponse>,
        marketResponse.json() as Promise<{ rows?: Parameters<typeof enrichSnapshotWithMarketHistory>[1] }>,
      ]);
      const nextReleaseId = snapshotResponse.headers.get("x-flow-release-id");
      if (expectedReleaseId && nextReleaseId !== expectedReleaseId) {
        throw new Error("release_pending");
      }
      const dates = Array.isArray(datesPayload.dates) ? [...datesPayload.dates] : [];
      if (!dates.includes(rawSnapshot.asOf)) dates.push(rawSnapshot.asOf);
      dates.sort();
      const enrichedBase = enrichSnapshotWithMarketHistory(
        rawSnapshot,
        Array.isArray(marketPayload.rows) ? marketPayload.rows : [],
      );
      baseSnapshotRef.current = enrichedBase;
      let nextSnapshot = enrichedBase;
      setViewLoadError(null);
      if (view && view !== "overview" && view !== "stock") {
        try {
          const data = await readView(view, enrichedBase, nextReleaseId, date, controller.signal);
          nextSnapshot = { ...enrichedBase, ...data } as DashboardSnapshot;
        } catch (cause: unknown) {
          setViewLoadError(cause instanceof Error ? cause.message : "视图数据加载失败");
        }
      }
      if (mountedRef.current) {
        snapshotRef.current = nextSnapshot;
        releaseIdRef.current = nextReleaseId ?? (date ? null : releaseIdRef.current);
        setReleaseId(nextReleaseId ?? (date ? null : releaseIdRef.current));
        setAvailableDates(dates);
        setSnapshot(nextSnapshot);
        setError(null);
      }
      return nextSnapshot;
    } catch (cause: unknown) {
      if (timedOut) throw new Error("request_timeout");
      throw cause;
    } finally {
      window.clearTimeout(timeoutId);
      if (requestRef.current === controller) requestRef.current = null;
    }
  }, [getRequestedDate, readView]);

  const loadView = useCallback(async (view: DeskView | "radar_flow") => {
    const base = baseSnapshotRef.current;
    if (!base) throw new Error("dashboard_not_ready");
    if (view === "stock") return;
    if (view === "overview") {
      viewRequestRef.current?.abort();
      viewRequestRef.current = null;
      if (mountedRef.current) {
        setViewLoadError(null);
        setViewLoading(false);
        snapshotRef.current = base;
        setSnapshot(base);
      }
      return;
    }
    const backgroundFlow = view === "radar_flow";
    if (backgroundFlow && base.etfStockFlow) return;
    const controller = new AbortController();
    viewRequestRef.current?.abort();
    viewRequestRef.current = controller;
    if (!backgroundFlow) {
      setViewLoading(true);
      setViewLoadError(null);
    }
    try {
      const data = await readView(view, base, releaseIdRef.current, getRequestedDate(), controller.signal);
      const current = snapshotRef.current;
      const nextSnapshot = view === "radar_flow" && current?.etfTerminal?.instruments?.length
        ? { ...base, etfTerminal: current.etfTerminal, ...data } as DashboardSnapshot
        : { ...base, ...data } as DashboardSnapshot;
      if (mountedRef.current && viewRequestRef.current === controller) {
        snapshotRef.current = nextSnapshot;
        setSnapshot(nextSnapshot);
      }
    } catch (cause: unknown) {
      if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
      if (mountedRef.current && viewRequestRef.current === controller && !backgroundFlow) setViewLoadError(cause instanceof Error ? cause.message : "视图数据加载失败");
      throw cause;
    } finally {
      if (viewRequestRef.current === controller) {
        viewRequestRef.current = null;
        if (mountedRef.current && !backgroundFlow) setViewLoading(false);
      }
    }
  }, [getRequestedDate, readView]);

  const loadRadarFlow = useCallback(
    () => loadView("radar_flow").then(() => snapshotRef.current?.etfStockFlow ?? null),
    [loadView],
  );

  const retryInitialLoad = useCallback(() => {
    setError(null);
    void loadSnapshot().catch((cause: unknown) => {
      if (cause instanceof DOMException && cause.name === "AbortError") return;
      if (mountedRef.current) setError(cause instanceof Error ? cause.message : "数据加载失败，请稍后重试");
    });
  }, [loadSnapshot]);

  const refreshLatest = useCallback(async (force = false) => {
    if (refreshInFlightRef.current) return;
    refreshInFlightRef.current = true;
    if (mountedRef.current) {
      setRefreshing(true);
      setRefreshError(null);
    }
    try {
      const date = getRequestedDate();
      let expectedReleaseId: string | undefined;
      if (!force && !date) {
        const pointerController = new AbortController();
        let pointerTimedOut = false;
        const pointerTimeoutId = window.setTimeout(() => {
          pointerTimedOut = true;
          pointerController.abort();
        }, REQUEST_TIMEOUT_MS);
        let pointerResponse: Response;
        try {
          pointerResponse = await fetch("/api/v2/releases/latest", {
            cache: "no-store",
            signal: pointerController.signal,
          });
        } catch (cause: unknown) {
          if (pointerTimedOut) throw new Error("request_timeout");
          throw cause;
        } finally {
          window.clearTimeout(pointerTimeoutId);
        }
        if (!pointerResponse.ok) throw new Error("release_check_failed");
        const pointer = await pointerResponse.json() as ReleasePointer;
        if (!pointer.releaseId || !pointer.asOf) throw new Error("release_check_failed");
        const sameRelease = Boolean(
          snapshotRef.current &&
          ((releaseIdRef.current && pointer.releaseId === releaseIdRef.current) ||
            (!releaseIdRef.current && pointer.asOf === snapshotRef.current.asOf)),
        );
        if (sameRelease) {
          refreshFailureCountRef.current = 0;
          return;
        }
        expectedReleaseId = pointer.releaseId;
      }
      await loadSnapshot(expectedReleaseId);
      refreshFailureCountRef.current = 0;
    } catch (cause: unknown) {
      if (cause instanceof DOMException && cause.name === "AbortError") return;
      refreshFailureCountRef.current += 1;
      if (mountedRef.current && refreshFailureCountRef.current >= REFRESH_ERROR_THRESHOLD) {
        setRefreshError(cause instanceof Error && cause.message === "release_pending"
          ? "新版本仍在发布，暂时保留当前完整数据"
          : "自动更新连续失败，已保留上一份完整数据；下次检查会自动重试");
      }
    } finally {
      refreshInFlightRef.current = false;
      if (mountedRef.current) setRefreshing(false);
    }
  }, [getRequestedDate, loadSnapshot]);

  useEffect(() => {
    mountedRef.current = true;
    // The initial request intentionally owns the bootstrap state transition.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadSnapshot().catch((cause: unknown) => {
      if (cause instanceof DOMException && cause.name === "AbortError") return;
      if (mountedRef.current) setError(cause instanceof Error ? cause.message : "数据加载失败，请稍后重试");
    });
    return () => {
      mountedRef.current = false;
      requestRef.current?.abort();
      viewRequestRef.current?.abort();
    };
  }, [loadSnapshot]);

  if (error) {
    return (
      <main className="terminal-shell bootstrap-state" role="alert">
        <div className="bootstrap-card">
          <strong>暂时无法读取完整数据</strong>
          <span>{error === "request_timeout" ? "数据请求超时，请稍后重试" : error}</span>
          <button type="button" onClick={retryInitialLoad}>重新加载</button>
        </div>
      </main>
    );
  }

  if (!snapshot) {
    return (
      <main className="terminal-shell bootstrap-state" aria-busy="true">
        <div className="bootstrap-card">
          <strong>正在加载已发布数据</strong>
          <span>只读取已经通过 QA 的完整数据快照；若网络暂时不可用，会保留当前数据并支持重试。</span>
        </div>
      </main>
    );
  }

  const params = new URLSearchParams(window.location.search);
  const requestedDateValue = params.get("date");
  if (params.get("view") === "stock") {
    return (
      <StockDetailView
        asOf={snapshot.asOf}
        code={params.get("code")}
        releaseId={releaseId}
        venue={params.get("venue")}
      />
    );
  }
  return (
    <Dashboard
      snapshot={snapshot}
      availableDates={availableDates}
      requestedDate={requestedDateValue && requestedDateValue !== snapshot.asOf ? requestedDateValue : null}
      exactDateMatch
      initialView={requestedView(params.get("view"))}
      onRefresh={() => refreshLatest(true)}
      onAutoRefresh={refreshLatest}
      onViewChange={loadView}
      onLoadRadarFlow={loadRadarFlow}
      viewLoadError={viewLoadError}
      viewLoading={viewLoading}
      refreshing={refreshing}
      refreshError={refreshError}
    />
  );
}
