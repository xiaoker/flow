"use client";

import { useEffect, useState } from "react";
import { Dashboard } from "./dashboard";
import { enrichSnapshotWithMarketHistory } from "./lib/market-history";
import type { DashboardSnapshot } from "./lib/dashboard-data";

type DeskView = "overview" | "rotation" | "radar" | "national" | "connect" | "margin";

type HistoryResponse = {
  dates?: string[];
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

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const requestedDate = params.get("date");
    const snapshotUrl = requestedDate
      ? `/api/v2/dashboard?date=${encodeURIComponent(requestedDate)}`
      : "/api/v2/dashboard";
    const historyUrl = requestedDate
      ? `/api/v2/market-history?asOf=${encodeURIComponent(requestedDate)}`
      : "/api/v2/market-history";
    const controller = new AbortController();

    Promise.all([
      fetch(snapshotUrl, { signal: controller.signal }),
      fetch("/api/v2/history/dates", { signal: controller.signal }),
      fetch(historyUrl, { signal: controller.signal }),
    ])
      .then(async ([snapshotResponse, datesResponse, marketResponse]) => {
        if (!snapshotResponse.ok) {
          throw new Error(snapshotResponse.status === 404 ? "该日期尚未形成完整发布版本" : `dashboard_${snapshotResponse.status}`);
        }
        if (!datesResponse.ok || !marketResponse.ok) throw new Error("history_unavailable");
        const [nextSnapshot, datesPayload, marketPayload] = await Promise.all([
          snapshotResponse.json() as Promise<DashboardSnapshot>,
          datesResponse.json() as Promise<HistoryResponse>,
          marketResponse.json() as Promise<{ rows?: Parameters<typeof enrichSnapshotWithMarketHistory>[1] }>,
        ]);
        const dates = Array.isArray(datesPayload.dates) ? [...datesPayload.dates] : [];
        if (!dates.includes(nextSnapshot.asOf)) dates.push(nextSnapshot.asOf);
        dates.sort();
        setAvailableDates(dates);
        setSnapshot(enrichSnapshotWithMarketHistory(nextSnapshot, Array.isArray(marketPayload.rows) ? marketPayload.rows : []));
      })
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        setError(cause instanceof Error ? cause.message : "数据加载失败，请稍后重试");
      });

    return () => controller.abort();
  }, []);

  if (error) {
    return (
      <main className="terminal-shell bootstrap-state" role="alert">
        <div className="bootstrap-card">
          <strong>暂时无法读取完整数据</strong>
          <span>{error}</span>
          <button type="button" onClick={() => window.location.reload()}>重新加载</button>
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
  const requestedDate = params.get("date");
  return (
    <Dashboard
      snapshot={snapshot}
      availableDates={availableDates}
      requestedDate={requestedDate && requestedDate !== snapshot.asOf ? requestedDate : null}
      exactDateMatch
      initialView={requestedView(params.get("view"))}
    />
  );
}
