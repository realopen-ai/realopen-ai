import { TooltipProvider } from "@/components/ui/tooltip";
import { ThemeManager } from "@/components/settings/ThemeManager";
import { AppLayout } from "@/components/layout/AppLayout";

export default function App() {
  return (
    <TooltipProvider>
      <ThemeManager />
      <AppLayout />
    </TooltipProvider>
  );
}
