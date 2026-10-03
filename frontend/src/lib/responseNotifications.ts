import { isAppActive } from "@/lib/appVisibility";
import { t, useSettingsStore } from "@/store/settingsStore";
import { useChatStore } from "@/store/chatStore";
import { playResponseCompleteSound } from "@/assets/sounds/response-complete";
import {
  completionAction,
  isViewingConversation,
  type ResponseOrigin,
} from "@/lib/completionNotificationPolicy";

export interface ResponseCompletion {
  conversationId: string;
  responseId: string;
  conversationTitle: string;
  origin: ResponseOrigin;
  responsePreview?: string;
}

export function completionPreview(content: string, maxLength = 180): string {
  const plain = content
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/[`*_>#\[\]]/g, "")
    .replace(/\s+/g, " ")
    .trim();
  if (plain.length <= maxLength) return plain;
  return `${plain.slice(0, maxLength - 1).trimEnd()}…`;
}

const handled = new Set<string>();
const STORAGE_KEY = "realopen-ai-notified-responses";

function wasHandled(id: string): boolean {
  if (handled.has(id)) return true;
  try {
    const ids = JSON.parse(sessionStorage.getItem(STORAGE_KEY) ?? "[]");
    if (Array.isArray(ids) && ids.includes(id)) {
      handled.add(id);
      return true;
    }
  } catch {
    // A malformed optional cache must never affect response completion.
  }
  return false;
}

function markHandled(id: string) {
  handled.add(id);
  try {
    const ids = Array.from(handled).slice(-200);
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(ids));
  } catch {
    // Storage may be unavailable in private/restricted browser contexts.
  }
}

function navigateToConversation(conversationId: string) {
  const path = `/${encodeURIComponent(conversationId)}`;
  if (window.location.pathname !== path) {
    window.history.pushState({}, "", path);
    window.dispatchEvent(new PopStateEvent("popstate"));
  }
}

export function notifyResponseCompleted(response: ResponseCompletion) {
  if (response.origin === "voice" || wasHandled(response.responseId)) return;
  markHandled(response.responseId);

  const appActive = isAppActive();
  const action = completionAction({
    origin: response.origin,
    appActive,
    viewingConversation: isViewingConversation(
      window.location.pathname,
      response.conversationId,
      useChatStore.getState().activeConversationId,
    ),
  });
  if (action === "none") return;

  const settings = useSettingsStore.getState();
  if (settings.completionSound) playResponseCompleteSound();

  if (action === "toast" && settings.inAppCompletionNotifications) {
    window.dispatchEvent(
      new CustomEvent("response-completion-toast", { detail: response }),
    );
    return;
  }

  if (
    action === "browser" &&
    settings.browserCompletionNotifications &&
    "Notification" in window &&
    Notification.permission === "granted"
  ) {
    try {
      const notification = new Notification(
        `${t("notifications.responseCompleted")} · ${response.conversationTitle}`,
        {
          body:
            response.responsePreview ||
            t("notifications.responseCompletedIn", {
              title: response.conversationTitle,
            }),
          tag: `response-${response.responseId}`,
        },
      );
      notification.onclick = () => {
        window.focus();
        navigateToConversation(response.conversationId);
        notification.close();
      };
    } catch {
      // OS/browser notification failures must not affect the completed turn.
    }
  }
}

export function openCompletedConversation(conversationId: string) {
  navigateToConversation(conversationId);
}
