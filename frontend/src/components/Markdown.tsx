import { Fragment } from "react";

/**
 * A deliberately tiny markdown renderer for `body_md`.
 *
 * Handles the subset the reasoning prompts actually emit — paragraphs, `-` bullets,
 * `**bold**`, and `*italic*` — and renders to React elements rather than HTML strings,
 * so model output can never inject markup. Pulling in a full markdown library plus a
 * sanitizer for four constructs is not a trade worth making here.
 *
 * If the prompts start emitting tables or links, replace this rather than extending it.
 */
export function Markdown({ text }: { text: string }) {
  const blocks = text.split(/\n\s*\n/);

  return (
    <div className="prose-body text-[13px]" style={{ color: "var(--text-muted)" }}>
      {blocks.map((block, blockIndex) => {
        const lines = block.split("\n");
        const isList = lines.every((line) => /^\s*[-*]\s+/.test(line));

        if (isList) {
          return (
            <ul key={blockIndex}>
              {lines.map((line, i) => (
                <li key={i}>{inline(line.replace(/^\s*[-*]\s+/, ""))}</li>
              ))}
            </ul>
          );
        }
        return <p key={blockIndex}>{inline(block.replace(/\n/g, " "))}</p>;
      })}
    </div>
  );
}

/** Split on bold/italic runs, keeping the delimiters' content. */
function inline(text: string): React.ReactNode[] {
  const parts = text.split(/(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`)/g);
  return parts.filter(Boolean).map((part, i) => {
    if (part.startsWith("**") && part.endsWith("**")) {
      return <strong key={i}>{part.slice(2, -2)}</strong>;
    }
    if (part.startsWith("`") && part.endsWith("`")) {
      return (
        <code key={i} className="font-mono text-[12px]">
          {part.slice(1, -1)}
        </code>
      );
    }
    if (part.startsWith("*") && part.endsWith("*")) {
      return <em key={i}>{part.slice(1, -1)}</em>;
    }
    return <Fragment key={i}>{part}</Fragment>;
  });
}
