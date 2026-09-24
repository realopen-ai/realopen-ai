interface TreeNode {
  name: string;
  type: "file" | "directory";
  size?: number;
  children?: TreeNode[];
}

function formatSize(size?: number): string {
  if (size === undefined) return "";
  if (size < 1024) return ` (${size} B)`;
  if (size < 1024 ** 2) return ` (${(size / 1024).toFixed(1)} KB)`;
  return ` (${(size / 1024 ** 2).toFixed(1)} MB)`;
}

function renderNodes(nodes: TreeNode[], prefix = ""): string[] {
  return nodes.flatMap((node, index) => {
    const last = index === nodes.length - 1;
    const branch = last ? "└── " : "├── ";
    const label = `${node.name}${node.type === "directory" ? "/" : formatSize(node.size)}`;
    const line = `${prefix}${branch}${label}`;
    const children = Array.isArray(node.children)
      ? renderNodes(node.children, `${prefix}${last ? "    " : "│   "}`)
      : [];
    return [line, ...children];
  });
}

/** Format only list-files envelopes; return all other file content verbatim. */
export function formatWorkspaceTreeOutput(content: string): string {
  try {
    const parsed: unknown = JSON.parse(content);
    if (
      !parsed ||
      typeof parsed !== "object" ||
      !("tree" in parsed) ||
      !Array.isArray((parsed as { tree?: unknown }).tree)
    ) {
      return content;
    }
    const tree = (parsed as { tree: TreeNode[] }).tree;
    if (
      !tree.every(
        (node) =>
          node &&
          typeof node.name === "string" &&
          (node.type === "file" || node.type === "directory"),
      )
    ) {
      return content;
    }
    return tree.length ? ["/workspace", ...renderNodes(tree)].join("\n") : "/workspace (empty)";
  } catch {
    return content;
  }
}
