import {
  createElement,
  memo,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import {
  highlightCode,
  type HighlightNode,
  type HighlightResult,
} from "@/lib/syntaxHighlighter";
import { cn } from "@/lib/utils";

function renderNode(node: HighlightNode, key: string): ReactNode {
  if (node.type === "text") return node.value;
  const properties = node.properties ?? {};
  const className = Array.isArray(properties.className)
    ? properties.className.join(" ")
    : properties.className;
  return createElement(
    node.tagName,
    { key, ...(className ? { className } : {}) },
    node.children.map((child, index) => renderNode(child, `${key}-${index}`)),
  );
}

export const HighlightedCode = memo(function HighlightedCode({
  code,
  language,
  filePath,
  className,
}: {
  code: string;
  language?: string;
  filePath?: string;
  className?: string;
}) {
  const [result, setResult] = useState<HighlightResult | null>(null);

  useEffect(() => {
    let current = true;
    setResult(null);
    const timer = window.setTimeout(() => {
      void highlightCode({ code, language, filePath }).then((next) => {
        if (current) setResult(next);
      });
    }, 75);
    return () => {
      current = false;
      window.clearTimeout(timer);
    };
  }, [code, language, filePath]);

  return (
    <code className={cn("starry-night-code", className)}>
      {result
        ? result.children.map((node, index) => renderNode(node, String(index)))
        : code}
    </code>
  );
});
