import { AccountHeader } from "@/components/AccountHeader";
import { CostPanel } from "@/components/CostPanel";
import { DecisionsTable } from "@/components/DecisionsTable";
import { EquityChart } from "@/components/EquityChart";
import { PositionsTable } from "@/components/PositionsTable";
import { TradesTable } from "@/components/TradesTable";
import { TreePanel } from "@/components/TreePanel";

export default function DashboardPage() {
  return (
    <div className="space-y-6">
      <AccountHeader />
      <EquityChart hours={24} />
      <CostPanel />
      <TreePanel />
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <PositionsTable />
        <DecisionsTable limit={50} />
      </div>
      <TradesTable limit={100} />
    </div>
  );
}
