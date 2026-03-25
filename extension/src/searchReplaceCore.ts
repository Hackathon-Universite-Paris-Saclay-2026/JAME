export interface SearchReplaceOptions {
  pattern: string;
  replacement: string;
  isRegex?: boolean;
  caseSensitive?: boolean;
  multiline?: boolean;
}

export interface TextEditSpan {
  startOffset: number;
  endOffset: number;
  matchedText: string;
  replacementText: string;
}

export function buildRegExp(opts: SearchReplaceOptions): RegExp {
  const source = opts.isRegex ? opts.pattern : escapeRegExp(opts.pattern);
  const flags = [
    "g",
    opts.caseSensitive === false ? "i" : "",
    opts.multiline ? "s" : "",
  ]
    .filter(Boolean)
    .join("");
  return new RegExp(source, flags);
}

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function computeReplacementText(
  fullMatch: string,
  match: RegExpExecArray,
  regex: RegExp,
  replacement: string,
  isRegex: boolean
): string {
  if (!isRegex) {
    return replacement;
  }

  // Build a non-global single-match regex so group replacements are computed
  // against this exact match without advancing shared state.
  const singleFlags = regex.flags.replace(/g/g, "");
  const singleRegex = new RegExp(regex.source, singleFlags);
  return fullMatch.replace(singleRegex, replacement);
}

export function computeTextEdits(
  text: string,
  opts: SearchReplaceOptions
): TextEditSpan[] {
  if (!opts.pattern) {
    return [];
  }

  const regex = buildRegExp(opts);
  regex.lastIndex = 0;

  const spans: TextEditSpan[] = [];
  let match: RegExpExecArray | null;

  while ((match = regex.exec(text)) !== null) {
    const matchedText = match[0];
    const startOffset = match.index;
    const endOffset = match.index + matchedText.length;
    const replacementText = computeReplacementText(
      matchedText,
      match,
      regex,
      opts.replacement,
      Boolean(opts.isRegex)
    );

    spans.push({
      startOffset,
      endOffset,
      matchedText,
      replacementText,
    });

    if (matchedText.length === 0) {
      regex.lastIndex += 1;
    }
  }

  return spans;
}
