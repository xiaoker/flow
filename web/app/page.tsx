import { Dashboard } from "./dashboard";
import { getDashboardViewData } from "./lib/dashboard-store";

export const dynamic = "force-dynamic";

export default async function Home({
  searchParams,
}: {
  searchParams: Promise<{ date?: string; view?: string }>;
}) {
  const { date, view } = await searchParams;
  const data = await getDashboardViewData(date);
  const initialView =
    view === "radar" || view === "national" || view === "connect" || view === "margin"
      ? view
      : undefined;
  return <Dashboard {...data} initialView={initialView} />;
}
