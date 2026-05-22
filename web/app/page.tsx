import { AccountHeader } from "@/components/AccountHeader";
import { CostPanel } from "@/components/CostPanel";
import { DecisionsTable } from "@/components/DecisionsTable";
import { EquityChart } from "@/components/EquityChart";
import { PositionsTable } from "@/components/PositionsTable";

export default function DashboardPage() {
  return (
    <div className="space-y-6">
      <AccountHeader />
      <EquityChart hours={24} />
      <CostPanel />
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <PositionsTable />
        <DecisionsTable limit={50} />
      </div>
    </div>
  );
}
