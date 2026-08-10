"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Dashboard } from "./dashboard";
import { enrichSnapshotWithMarketHistory } from "./lib/market-history";
import type { DashboardSnapshot } from "./lib/dashboard-data";

type DeskView = "overview" | "rotation" | "radar" | "national" | "connect" | "margin";

type HistoryResponse = {
  dates?: string[];
};

type ReleasePointer = {
  releaseId?: string;
  asOf?: string;
};

function requestedView(value: string | null): DeskView | undefined {
  return value === "rotation" || value === "radar" || value === "national" || value === "connect" || value === "margin"
    ? value
    : undefined;
}

export function DashboardBootstrap() {
  const [snapshot, setSnapshot] = useState<DashboardSnapshot | null>(null);
  const [availableDates, setAvailableDates] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const snapshotRef = useRef<DashboardSnapshot | null>(null);
  const releaseIdRef = useRef<string | null>(null);
  const requestRef = useRef<AbortController | null>(null);
  const refreshInFlightRef = useRef(false);
  const mountedRef = useRef(true);

  const getRequestedDate = useCallback(() => {
    if (typeof window === "undefined") return null;
    return new URLSearchParams(window.location.search).get("date");
  }, []);

  const loadSnapshot = useCallback(async (expectedReleaseId?: string) => {
    const date = getRequestedDate();
    const snapshotUrl = date
      ? `/api/v2/dashboard?date=${encodeURIComponent(date)}`
      : "/api/v2/dashboard";
    const historyUrl = date
      ? `/api/v2/market-history?asOf=${encodeURIComponent(date)}`
      : "/api/v2/market-history";
    requestRef.current?.abort();
    const controller = new AbortController();
    requestRef.current = controller;

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
      const [nextSnapshot, datesPayload, marketPayload] = await Promise.all([
        snapshotResponse.json() as Promise<DashboardSnapshot>,
        datesResponse.json() as Promise<HistoryResponse>,
        marketResponse.json() as Promise<{ rows?: Parameters<typeof enrichSnapshotWithMarketHistory>[1] }>,
      ]);
      const nextReleaseId = snapshotResponse.headers.get("x-flow-release-id");
      if (expectedReleaseId && nextReleaseId !== expectedReleaseId) {
        throw new Error("release_pending");
      }
      const dates = Array.isArray(datesPayload.dates) ? [...datesPayload.dates] : [];
      if (!dates.includes(nextSnapshot.asOf)) dates.push(nextSnapshot.asOf);
      dates.sort();
      const enrichedSnapshot = enrichSnapshotWithMarketHistory(
        nextSnapshot,
        Array.isArray(marketPayload.rows) ? marketPayload.rows : [],
      );
      if (mountedRef.current) {
        snapshotRef.current = enrichedSnapshot;
        releaseIdRef.current = nextReleaseId ?? (date ? null : releaseIdRef.current);
        setAvailableDates(dates);
        setSnapshot(enrichedSnapshot);
        setError(null);
      }
      return enrichedSnapshot;
    } finally {
      if (requestRef.current === controller) requestRef.current = null;
    }
  }, [getRequestedDate]);

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
        const pointerResponse = await fetch("/api/v2/releases/latest", { cache: "no-store" });
        if (!pointerResponse.ok) throw new Error("release_check_failed");
        const pointer = await pointerResponse.json() as ReleasePointer;
        if (!pointer.releaseId || !pointer.asOf) throw new Error("release_check_failed");
        const sameRelease = Boolean(
          snapshotRef.current &&
          ((releaseIdRef.current && pointer.releaseId === releaseIdRef.current) ||
            (!releaseIdRef.current && pointer.asOf === snapshotRef.current.asOf)),
        );
        if (sameRelease) return;
        expectedReleaseId = pointer.releaseId;
      }
      await loadSnapshot(expectedReleaseId);
    } catch (cause: unknown) {
      if (cause instanceof DOMException && cause.name === "AbortError") return;
      if (mountedRef.current) {
        setRefreshError(cause instanceof Error && cause.message === "release_pending"
          ? "新版本正在发布，暂时保留当前数据"
          : "自动更新暂时失败，继续显示上一份完整数据");
      }
    } finally {
      refreshInFlightRef.current = false;
      if (mountedRef.current) setRefreshing(false);
    }
  }, [getRequestedDate, loadSnapshot]);

  useEffect(() => {
    mountedRef.current = true;
    void loadSnapshot().catch((cause: unknown) => {
      if (cause instanceof DOMException && cause.name === "AbortError") return;
      if (mountedRef.current) setError(cause instanceof Error ? cause.message : "数据加载失败，请稍后重试");
    });
    return () => {
      mountedRef.current = false;
      requestRef.current?.abort();
    };
  }, [loadSnapshot]);

  if (error) {
    return (
      <main className="terminal-shell bootstrap-state" role="alert">
        <div className="bootstrap-card">
          <strong>暂时无法读取完整数据</strong>
          <span>{error}</span>
          <button type="button" onClick={retryInitialLoad}>重新加载</button>
        </div>
      </main>
    );
  }

  if (!snapshot) {
    return (
      <main className="terminal-shell bootstrap-state" aria-busy="true">
        <div className="bootstrap-card">
          <strong>正在读取最新发布版本</strong>
          <span>只读取已经通过 QA 的完整数据，不在 Worker 请求中重算 ETF 状态。</span>
        </div>
      </main>
    );
  }

  const params = new URLSearchParams(window.location.search);
  const requestedDateValue = params.get("date");
  return (
    <Dashboard
      snapshot={snapshot}
      availableDates={availableDates}
      requestedDate={requestedDateValue && requestedDateValue !== snapshot.asOf ? requestedDateValue : null}
      exactDateMatch
      initialView={requestedView(params.get("view"))}
      onRefresh={() => refreshLatest(true)}
      onAutoRefresh={refreshLatest}
      refreshing={refreshing}
      refreshError={refreshError}
    />
  );
}
