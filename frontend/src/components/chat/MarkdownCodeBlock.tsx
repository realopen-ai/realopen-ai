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
    <div className="chat-code-block my-3 overflow-hidden rounded-[10px] border border-border bg-sandbox-bg">
      <div className="flex h-8 items-center justify-between border-b border-border bg-card px-3">
        <span className="font-mono text-[10px] text-muted-foreground">
          {language || "code"}
        </span>
        <button
          type="button"
          onClick={() => void copy()}
          className="flex items-center gap-1 rounded px-1.5 py-1 text-[10px] text-muted-foreground hover:bg-accent hover:text-foreground"
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
