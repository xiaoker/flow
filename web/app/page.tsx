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
  return <Dashboard {...data} initialView={view === "national" ? "national" : undefined} />;
}
