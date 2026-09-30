import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  highlightCode,
  MAX_HIGHLIGHT_BYTES,
  type HighlightNode,
} from "../src/lib/syntaxHighlighter.ts";

function flatten(nodes: HighlightNode[]): { text: string; classes: string[] } {
  let text = "";
  const classes: string[] = [];
  const visit = (node: HighlightNode) => {
    if (node.type === "text") {
      text += node.value;
      return;
    }
    const value = node.properties?.className;
    if (Array.isArray(value)) classes.push(...value.map(String));
    else if (value) classes.push(String(value));
    node.children.forEach(visit);
  };
  nodes.forEach(visit);
  return { text, classes };
}

test("highlights fenced code from a language flag", async () => {
  const code = 'const greeting = "hello";';
  const result = await highlightCode({ code, language: "typescript" });
  assert.ok(result);
  const rendered = flatten(result.children);
  assert.equal(rendered.text, code);
  assert.ok(rendered.classes.length > 0);
});

test("detects a grammar from a sandbox filename", async () => {
  const code = "def health():\n    return {'status': 'ok'}\n";
  const result = await highlightCode({
    code,
    filePath: "/workspace/service/main.py",
  });
  assert.ok(result);
  assert.equal(flatten(result.children).text, code);
});

test("keeps unknown languages and oversized files as plain-text fallbacks", async () => {
  assert.equal(
    await highlightCode({ code: "plain text", filePath: "notes.customxyz" }),
    null,
  );
  assert.equal(
    await highlightCode({ code: "hello", language: "not-a-real-language" }),
    null,
  );
  assert.equal(
    await highlightCode({
      code: "x".repeat(MAX_HIGHLIGHT_BYTES + 1),
      language: "javascript",
    }),
    null,
  );
});

test("chat and file views retain their controls and use the shared highlighter", () => {
  const chat = readFileSync(
    new URL("../src/components/chat/MarkdownCodeBlock.tsx", import.meta.url),
    "utf8",
  );
  const files = readFileSync(
    new URL(
      "../src/components/file-explorer/FileExplorer.tsx",
      import.meta.url,
    ),
    "utf8",
  );
  assert.match(chat, /Copy code/);
  assert.match(chat, /<HighlightedCode code=\{code\} language=\{language\}/);
  assert.match(
    files,
    /<HighlightedCode code=\{draft\} filePath=\{activeFile\}/,
  );
  assert.match(files, /t\("panel\.save"\)/);
});

test("read, write, and code-exec tool details use shared highlighting metadata", () => {
  const messageBubble = readFileSync(
    new URL("../src/components/chat/MessageBubble.tsx", import.meta.url),
    "utf8",
  );
  assert.match(
    messageBubble,
    /code=\{displayedContent\}[\s\S]*filePath=\{tc\.filePath\}/,
  );
  assert.match(
    messageBubble,
    /tc\.type === "file_read" \|\| tc\.type === "file_write"/,
  );
  assert.match(
    messageBubble,
    /code=\{tc\.code\}[\s\S]*language=\{tc\.language \?\? "python"\}/,
  );
  assert.match(messageBubble, /line\.startsWith\("---"\)/);
  assert.match(messageBubble, /bg-danger\/10 text-danger/);
  assert.match(messageBubble, /line\.startsWith\("\+\+\+"\)/);
  assert.match(messageBubble, /bg-success\/10 text-success/);
});

test("skill instructions and resources use shared syntax highlighting", () => {
  const skills = readFileSync(
    new URL("../src/components/brain/SkillsTab.tsx", import.meta.url),
    "utf8",
  );
  assert.match(skills, /components=\{markdownCodeComponents\}/);
  assert.match(
    skills,
    /code=\{resourceContent \?\? "Loading resource…"\}[\s\S]*filePath=\{selectedResource\}/,
  );
});

test("defines readable Starry Night palettes for both application themes", () => {
  const css = readFileSync(
    new URL("../src/index.css", import.meta.url),
    "utf8",
  );
  assert.match(
    css,
    /:root\s*\{[\s\S]*--color-prettylights-syntax-keyword: #cf222e/,
  );
  assert.match(
    css,
    /html\.dark\s*\{[\s\S]*--color-prettylights-syntax-keyword: #ff7b72/,
  );
});

test("preserves markup-like source as text rather than executable HTML", async () => {
  const code = '<script>alert("nope")</script>';
  const result = await highlightCode({ code, language: "html" });
  assert.ok(result);
  assert.equal(flatten(result.children).text, code);
});

test("does not reuse stale output when switching file contents", async () => {
  const first = await highlightCode({
    code: "const first = 1",
    filePath: "a.ts",
  });
  const second = await highlightCode({
    code: "const second = 2",
    filePath: "b.ts",
  });
  assert.ok(first && second);
  assert.equal(flatten(first.children).text, "const first = 1");
  assert.equal(flatten(second.children).text, "const second = 2");
});
