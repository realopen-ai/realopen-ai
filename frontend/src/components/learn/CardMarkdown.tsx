import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { markdownCodeComponents } from "@/components/chat/MarkdownCodeBlock";

export function CardMarkdown({ text }: { text: string }) {
  return (
    <div
      dir="auto"
      className="prose prose-sm dark:prose-invert max-w-none break-words text-start [&_pre]:overflow-x-auto [&_p]:my-2"
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={markdownCodeComponents}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
