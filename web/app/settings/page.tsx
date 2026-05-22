import { LeverageForm } from "@/components/LeverageForm";
import { PauseToggle } from "@/components/PauseToggle";
import { RiskForm } from "@/components/RiskForm";

export default function SettingsPage() {
  return (
    <div className="space-y-6">
      <PauseToggle />
      <LeverageForm />
      <RiskForm />
    </div>
  );
}
