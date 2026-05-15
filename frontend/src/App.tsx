import { Routes, Route } from "react-router-dom";
import { TooltipProvider } from "@/components/ui/tooltip";
import { ThemeManager } from "@/components/settings/ThemeManager";
import { AppLayout } from "@/components/layout/AppLayout";

export default function App() {
  return (
    <TooltipProvider>
      <ThemeManager />
      <Routes>
        <Route path="/" element={<AppLayout />} />
        <Route path="/:conversationId" element={<AppLayout />} />
      </Routes>
    </TooltipProvider>
  );
}
