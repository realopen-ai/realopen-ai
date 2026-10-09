import { useEffect, useState } from "react";
import { Routes, Route } from "react-router-dom";
import { TooltipProvider } from "@/components/ui/tooltip";
import { ThemeManager } from "@/components/settings/ThemeManager";
import { AppLayout } from "@/components/layout/AppLayout";
import { SetupWizard } from "@/components/setup/SetupWizard";
import { fetchSetupStatus } from "@/api/setupClient";
import { ResponseCompletionToasts } from "@/components/ui/ResponseCompletionToasts";

export default function App() {
  const [setupNeeded, setSetupNeeded] = useState<boolean | null>(null);

  useEffect(() => {
    // Check if setup is needed on app load
    fetchSetupStatus()
      .then((status) => {
        setSetupNeeded(!status.setup_complete);
      })
      .catch(() => {
        // If API fails, assume setup is needed (first run)
        setSetupNeeded(true);
      });
  }, []);

  // Still checking
  if (setupNeeded === null) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-background">
        <div className="flex flex-col items-center gap-3">
          <div className="w-8 h-8 border-2 border-primary border-t-transparent rounded-full animate-spin" />
          <p className="text-[14px] text-muted-foreground">Loading...</p>
        </div>
      </div>
    );
  }

  // Show setup wizard if setup hasn't been completed
  if (setupNeeded) {
    return (
      <TooltipProvider>
        <ThemeManager />
        <SetupWizard />
      </TooltipProvider>
    );
  }

  // Normal app
  return (
    <TooltipProvider>
      <ThemeManager />
      <ResponseCompletionToasts />
      <Routes>
        <Route path="/" element={<AppLayout />} />
        <Route path="/learn" element={<AppLayout />} />
        <Route path="/learn/notes" element={<AppLayout />} />
        <Route path="/learn/notes/:noteId" element={<AppLayout />} />
        <Route path="/learn/flashcards" element={<AppLayout />} />
        <Route path="/learn/review" element={<AppLayout />} />
        <Route path="/learn/flashcards/:deckId" element={<AppLayout />} />
        <Route path="/learn/flashcards/:deckId/study" element={<AppLayout />} />
        <Route path="/brain" element={<AppLayout />} />
        <Route path="/brain/memories" element={<AppLayout />} />
        <Route path="/brain/history" element={<AppLayout />} />
        <Route path="/brain/skills" element={<AppLayout />} />
        <Route path="/brain/tools" element={<AppLayout />} />
        <Route path="/brain/tools/:toolId" element={<AppLayout />} />
        <Route path="/workspace" element={<AppLayout />} />
        <Route path="/workspace/documents" element={<AppLayout />} />
        <Route path="/workspace/artifacts" element={<AppLayout />} />
        <Route
          path="/workspace/artifacts/:artifactId"
          element={<AppLayout />}
        />
        <Route path="/workspace/templates" element={<AppLayout />} />
        <Route path="/workspace/generated" element={<AppLayout />} />
        <Route path="/workspace/assets" element={<AppLayout />} />
        <Route path="/workspace/sandboxes" element={<AppLayout />} />
        <Route path="/:conversationId" element={<AppLayout />} />
      </Routes>
    </TooltipProvider>
  );
}
