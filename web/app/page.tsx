import { DashboardBootstrap } from "./dashboard-bootstrap";

// The route is a static shell. Data is fetched client-side from the v2
// release API, so normal navigation never SSRs a large dashboard snapshot.
export const dynamic = "force-static";

export default function Home() {
  return <DashboardBootstrap />;
}
