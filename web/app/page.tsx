import { Dashboard } from "./dashboard";
import { dashboardSnapshot } from "./lib/dashboard-data";

export default function Home() {
  return <Dashboard snapshot={dashboardSnapshot} />;
}
