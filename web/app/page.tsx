import { Dashboard } from "./dashboard";
import { getDashboardViewData } from "./lib/dashboard-store";

export const dynamic = "force-dynamic";

export default async function Home({
  searchParams,
}: {
  searchParams: Promise<{ date?: string }>;
}) {
  const { date } = await searchParams;
  const data = await getDashboardViewData(date);
  return <Dashboard {...data} />;
}
