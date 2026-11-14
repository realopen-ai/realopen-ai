import { ChevronDown, MessageSquarePlus } from "lucide-react";
import { useNavigate } from "react-router-dom";
import {
  artifactChatCommands,
  type ArtifactChatDraft,
  type ArtifactDetail,
  type ArtifactSection,
} from "@/api/artifactsClient";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useT } from "@/store/settingsStore";
import { useUIStore } from "@/store/uiStore";

export function ArtifactChatMenu({
  artifact,
  section,
  disabled,
  beforeChoose,
  defaultOpen,
}: {
  artifact: ArtifactDetail;
  section?: ArtifactSection;
  disabled?: boolean;
  beforeChoose?: () => boolean;
  defaultOpen?: boolean;
}) {
  const t = useT();
  const navigate = useNavigate();
  if (!artifact.conversation_id && !artifact.document_id) return null;
  function choose(
    command: ArtifactChatDraft["command"],
    target?: ArtifactSection,
  ) {
    if (beforeChoose && !beforeChoose()) return;
    const artifactChatDraft: ArtifactChatDraft = {
      conversationId: artifact.conversation_id,
      command,
      title: [artifact.title, `v${artifact.selected_version}`, target?.title]
        .filter(Boolean)
        .join(" · "),
      source: {
        artifact_id: artifact.id,
        version: artifact.selected_version,
        section_id: target?.id,
      },
    };
    useUIStore.getState().setMobileTab("chat");
    navigate(artifact.conversation_id ? `/${artifact.conversation_id}` : "/", {
      state: { artifactChatDraft },
    });
  }
  return (
    <DropdownMenu defaultOpen={defaultOpen}>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm" disabled={disabled}>
          <MessageSquarePlus className="size-4" />
          {t("artifacts.useInChat")}
          <ChevronDown className="size-3.5 text-muted-foreground" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        className="max-h-[var(--radix-dropdown-menu-content-available-height)] overflow-auto"
      >
        <DropdownMenuLabel>{t("artifacts.wholeFile")}</DropdownMenuLabel>
        {artifactChatCommands.map((command) => (
          <DropdownMenuItem key={command} onSelect={() => choose(command)}>
            <span className="font-mono">/{command}</span>
          </DropdownMenuItem>
        ))}
        {section && (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuLabel
              className="max-w-64 truncate"
              title={section.title}
            >
              {t("artifacts.currentSection", { title: section.title })}
            </DropdownMenuLabel>
            {artifactChatCommands.map((command) => (
              <DropdownMenuItem
                key={command}
                onSelect={() => choose(command, section)}
              >
                <span className="font-mono">/{command}</span>
              </DropdownMenuItem>
            ))}
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
