import { isValidElement, useState, type ReactNode } from "react";
import { Check, Copy } from "lucide-react";
import { HighlightedCode } from "@/components/ui/HighlightedCode";

export function MarkdownCode({
  className,
  children,
}: {
  className?: string;
  children?: ReactNode;
  node?: unknown;
}) {
  return <code className={className}>{children}</code>;
}

export function MarkdownPre({ children }: { children?: ReactNode }) {
  const [copied, setCopied] = useState(false);
  if (!isValidElement<{ className?: string; children?: ReactNode }>(children))
    return <pre>{children}</pre>;
  const code = String(children.props.children ?? "").replace(/\n$/, "");
  const language = children.props.className?.match(/language-([^\s]+)/)?.[1];
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      // Clipboard access can be unavailable outside a secure browser context.
    }
  };
  return (
    <div className="chat-code-block my-3 overflow-hidden rounded-lg border border-border/60 bg-sandbox-bg">
      <div className="flex h-8 items-center justify-between border-b border-border/40 bg-card/60 px-3">
        <span className="font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
          {language || "code"}
        </span>
        <button
          type="button"
          onClick={() => void copy()}
          className="flex items-center gap-1 rounded-md px-1.5 py-1 text-[11px] text-muted-foreground hover:bg-surface-hover hover:text-foreground transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          aria-label="Copy code"
          title="Copy code"
        >
          {copied ? (
            <Check className="h-3 w-3" />
          ) : (
            <Copy className="h-3 w-3" />
          )}
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      {/* padding is enforced by .prose .chat-code-block pre (12px) in index.css */}
      <pre className="m-0 overflow-x-auto border-0 bg-transparent p-3">
        <HighlightedCode code={code} language={language} />
      </pre>
    </div>
  );
}

export const markdownCodeComponents = {
  code: MarkdownCode,
  pre: MarkdownPre,
};
