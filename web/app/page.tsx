import { Dashboard } from "./dashboard";
import { getDashboardSnapshot } from "./lib/dashboard-store";

export const dynamic = "force-dynamic";

export default async function Home() {
  return <Dashboard snapshot={await getDashboardSnapshot()} />;
}
