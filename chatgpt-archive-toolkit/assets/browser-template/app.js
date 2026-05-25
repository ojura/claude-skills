const EXPORT_ROOT = "..";
const FILE_ID_RE = /\bfile_[0-9a-fA-F]{24,}\b/g;
const BIG_CODE_CHARS = 2400;

const state = {
  view: "overview",
  query: "",
  selectedConversationId: null,
  selectedFileId: null,
  selectedEndpoint: null,
  fileKind: "all",
  showHidden: false,
  showSystem: false,
  showRaw: false,
  showAllNodes: false,
  showTechnical: false,
  conversationLimit: 350,
  branchChoices: new Map(),
};

const data = {
  manifest: null,
  conversations: [],
  conversationBodies: new Map(),
  libraryNodes: [],
  fileDownloads: [],
  artifactRefs: [],
  endpoints: [],
  tasks: null,
  mediaDownloads: [],
  fileById: new Map(),
  libraryByFileId: new Map(),
  endpointByName: new Map(),
};

const $ = (id) => document.getElementById(id);

function node(tag, props = {}, ...children) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "className") element.className = value;
    else if (key === "text") element.textContent = value;
    else if (key === "title") element.title = value;
    else if (key === "htmlFor") element.htmlFor = value;
    else if (key.startsWith("on") && typeof value === "function") {
      element.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === "dataset") {
      for (const [dataKey, dataValue] of Object.entries(value)) element.dataset[dataKey] = dataValue;
    } else if (key === "attrs") {
      for (const [attrKey, attrValue] of Object.entries(value)) element.setAttribute(attrKey, attrValue);
    } else if (key === "checked") {
      element.checked = Boolean(value);
    } else {
      element.setAttribute(key, value);
    }
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    if (typeof child === "string" || typeof child === "number") element.append(document.createTextNode(String(child)));
    else element.append(child);
  }
  return element;
}

async function loadJson(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error(`${response.status} ${path}`);
  return response.json();
}

function relativePath(path) {
  if (!path) return null;
  const root = data.manifest?.root || "";
  let rel = String(path);
  if (rel.startsWith(root + "/")) rel = rel.slice(root.length + 1);
  if (rel.startsWith("/")) return null;
  return rel;
}

function hrefForPath(path) {
  const rel = relativePath(path);
  if (!rel) return null;
  return `../${rel.split("/").map(encodeURIComponent).join("/")}`;
}

function formatDate(value) {
  if (!value) return "";
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function formatBytes(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "";
  const units = ["B", "KB", "MB", "GB"];
  let size = Number(value);
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size >= 10 || unit === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[unit]}`;
}

function short(value, length = 10) {
  if (!value) return "";
  const text = String(value);
  return text.length > length ? `${text.slice(0, length)}...` : text;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

async function copyText(value) {
  const text = String(value ?? "");
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = node("textarea", { text });
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  document.body.append(textarea);
  textarea.focus();
  textarea.select();
  document.execCommand("copy");
  textarea.remove();
}

function copyButton(label, getText) {
  return node("button", {
    className: "copy-button",
    type: "button",
    title: label,
    text: label,
    onClick: async (event) => {
      event.preventDefault();
      event.stopPropagation();
      const button = event.currentTarget;
      const original = button.textContent;
      try {
        await copyText(typeof getText === "function" ? getText() : getText);
        button.textContent = "Copied";
        setTimeout(() => {
          button.textContent = original;
        }, 1100);
      } catch (error) {
        button.textContent = "Failed";
        console.error(error);
        setTimeout(() => {
          button.textContent = original;
        }, 1400);
      }
    },
  });
}

function languageFromName(name = "") {
  const lower = String(name).toLowerCase();
  if (lower.endsWith(".json")) return "json";
  if (lower.endsWith(".js") || lower.endsWith(".mjs") || lower.endsWith(".ts") || lower.endsWith(".tsx")) return "javascript";
  if (lower.endsWith(".py")) return "python";
  if (lower.endsWith(".css")) return "css";
  if (lower.endsWith(".html") || lower.endsWith(".htm") || lower.endsWith(".xml") || lower.endsWith(".svg")) return "html";
  if (lower.endsWith(".md")) return "markdown";
  if (lower.endsWith(".sh") || lower.endsWith(".bash") || lower.endsWith(".zsh")) return "shell";
  if (lower.endsWith(".toml") || lower.endsWith(".yaml") || lower.endsWith(".yml")) return "config";
  return "text";
}

function span(className, text) {
  return `<span class="${className}">${escapeHtml(text)}</span>`;
}

function highlightJson(code) {
  return String(code).replace(
    /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false)\b|\bnull\b|-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b/g,
    (match, stringToken, colon, boolToken) => {
      if (stringToken) return `${span(colon ? "tok-key" : "tok-string", stringToken)}${colon || ""}`;
      if (boolToken) return span("tok-boolean", match);
      if (match === "null") return span("tok-null", match);
      return span("tok-number", match);
    }
  );
}

function highlightHtml(code) {
  const escaped = escapeHtml(code);
  return escaped.replace(/(&lt;\/?)([A-Za-z][\w:.-]*)([\s\S]*?)(\/?&gt;)/g, (_match, open, tag, attrs, close) => {
    const highlightedAttrs = attrs.replace(/([A-Za-z_:][-A-Za-z0-9_:.]*)(=)(&quot;.*?&quot;|&#39;.*?&#39;|[^\s&]+)?/g, (_attr, name, eq, value = "") => {
      return `${span("tok-attr", name)}${eq}${span("tok-string", value)}`;
    });
    return `${open}${span("tok-tag", tag)}${highlightedAttrs}${close}`;
  });
}

function highlightGeneric(code, language) {
  const keywords = {
    javascript: /\b(async|await|break|case|catch|class|const|continue|default|else|export|for|from|function|if|import|let|new|return|switch|throw|try|typeof|var|while|yield)\b/g,
    python: /\b(and|as|assert|break|class|continue|def|elif|else|except|False|finally|for|from|if|import|in|is|lambda|None|not|or|pass|raise|return|True|try|while|with|yield)\b/g,
    shell: /\b(case|do|done|elif|else|esac|fi|for|function|if|in|then|while)\b/g,
    css: /\b(@media|@supports|display|grid|flex|block|none|relative|absolute|fixed|sticky|var|calc|repeat|minmax)\b/g,
    markdown: /(`[^`]+`|\*\*[^*]+\*\*|^#{1,6}\s.*$)/gm,
    config: /\b(true|false|null)\b/g,
  };
  const tokenRe = /\/\*[\s\S]*?\*\/|\/\/[^\n]*|#[^\n]*|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b/g;
  let out = "";
  let last = 0;
  for (const match of String(code).matchAll(tokenRe)) {
    out += escapeHtml(code.slice(last, match.index));
    const token = match[0];
    if (token.startsWith("//") || token.startsWith("/*") || token.startsWith("#")) out += span("tok-comment", token);
    else if (/^[-\d]/.test(token)) out += span("tok-number", token);
    else out += span("tok-string", token);
    last = match.index + token.length;
  }
  out += escapeHtml(code.slice(last));
  const keywordRe = keywords[language];
  if (keywordRe) {
    out = out.replace(keywordRe, (match) => `<span class="tok-keyword">${match}</span>`);
  }
  out = out.replace(/\b(fetch|Promise|Array|Object|String|Number|JSON|console|document|window|print|open|len|dict|list|set)\b/g, '<span class="tok-builtin">$1</span>');
  return out;
}

function highlightCode(code, language = "text") {
  const normalized = languageFromName(`x.${language}`) === "text" ? String(language || "text").toLowerCase() : languageFromName(`x.${language}`);
  if (normalized === "json") return highlightJson(code);
  if (normalized === "html") return highlightHtml(code);
  if (normalized === "text") return escapeHtml(code);
  return highlightGeneric(code, normalized);
}

function codeBlock(text, language = "text", label = null, options = {}) {
  const code = String(text ?? "");
  const lang = language || "text";
  const collapsed = options.collapsed ?? code.length > BIG_CODE_CHARS;
  const pre = node("pre", { className: "syntax" });
  pre.innerHTML = highlightCode(code, lang);
  const frame = node(
    "div",
    { className: "code-frame" },
    node(
      "div",
      { className: "code-header" },
      node("span", { className: "code-label", text: label || lang }),
      node("div", { className: "code-actions" }, node("span", { className: "char-count", text: `${code.length.toLocaleString()} chars` }), copyButton("Copy", () => pre.textContent || code))
    ),
    pre
  );
  if (!collapsed) return frame;
  return node(
    "details",
    { className: "code-details" },
    node(
      "summary",
      {},
      node("span", { text: label || lang }),
      node("span", { className: "summary-meta", text: `${code.length.toLocaleString()} chars · collapsed` })
    ),
    frame
  );
}

function jsonBlock(value, limit = 40000) {
  let text = JSON.stringify(value, null, 2);
  if (text.length > limit) text = `${text.slice(0, limit)}\n... truncated ${text.length - limit} chars`;
  return codeBlock(text, "json", "json");
}

function detailsBlock(label, value, open = false) {
  return node("details", open ? { attrs: { open: "" } } : {}, node("summary", { text: label }), jsonBlock(value));
}

function safeHref(url) {
  const href = String(url || "").trim();
  if (/^(https?:|mailto:|#|\.{0,2}\/)/i.test(href)) return href;
  return "#";
}

function renderInlineMarkdown(value) {
  const citationSpans = [];
  const withCitations = String(value).replace(/\uE200filecite\uE202([^\uE201]+)\uE201/g, (_match, target) => {
    const index = citationSpans.length;
    const label = String(target).replaceAll("\uE202", " ");
    citationSpans.push(`<span class="citation-marker">${escapeHtml(label)}</span>`);
    return `\u0001${index}\u0001`;
  });
  const codeSpans = [];
  let html = escapeHtml(withCitations).replace(/`([^`\n]+)`/g, (_match, code) => {
    const index = codeSpans.length;
    codeSpans.push(`<code>${code}</code>`);
    return `\u0000${index}\u0000`;
  });
  html = html.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_match, label, href) => {
    return `<a href="${escapeHtml(safeHref(href))}" target="_blank" rel="noreferrer">${label}</a>`;
  });
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/__([^_]+)__/g, "<strong>$1</strong>");
  html = html.replace(/(^|[^\*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  html = html.replace(/(^|[^_])_([^_\n]+)_/g, "$1<em>$2</em>");
  html = html.replace(/\u0000(\d+)\u0000/g, (_match, index) => codeSpans[Number(index)] || "");
  html = html.replace(/\u0001(\d+)\u0001/g, (_match, index) => citationSpans[Number(index)] || "");
  return html;
}

function markdownSpecialLine(line) {
  return (
    /^#{1,6}\s+/.test(line) ||
    /^\s*([-*+])\s+/.test(line) ||
    /^\s*\d+\.\s+/.test(line) ||
    /^\s*>\s?/.test(line) ||
    isTableStart(line)
  );
}

function isTableStart(line, nextLine = "") {
  return /\|/.test(line) && /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(nextLine);
}

function tableCells(line) {
  let text = line.trim();
  if (text.startsWith("|")) text = text.slice(1);
  if (text.endsWith("|")) text = text.slice(0, -1);
  return text.split("|").map((cell) => cell.trim());
}

function appendMarkdownLines(parent, text) {
  const lines = String(text || "").replace(/\r\n?/g, "\n").split("\n");
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      const level = Math.min(6, heading[1].length);
      const element = node(`h${level}`, {});
      element.innerHTML = renderInlineMarkdown(heading[2]);
      parent.append(element);
      index += 1;
      continue;
    }

    if (isTableStart(line, lines[index + 1])) {
      const table = node("table", { className: "markdown-table" });
      const headRow = node("tr", {}, tableCells(line).map((cell) => {
        const th = node("th", {});
        th.innerHTML = renderInlineMarkdown(cell);
        return th;
      }));
      const bodyRows = [];
      index += 2;
      while (index < lines.length && /\|/.test(lines[index]) && lines[index].trim()) {
        bodyRows.push(
          node("tr", {}, tableCells(lines[index]).map((cell) => {
            const td = node("td", {});
            td.innerHTML = renderInlineMarkdown(cell);
            return td;
          }))
        );
        index += 1;
      }
      table.append(node("thead", {}, headRow), node("tbody", {}, bodyRows));
      parent.append(node("div", { className: "markdown-table-wrap" }, table));
      continue;
    }

    const unordered = line.match(/^\s*([-*+])\s+(.+)$/);
    const ordered = line.match(/^\s*\d+\.\s+(.+)$/);
    if (unordered || ordered) {
      const list = node(unordered ? "ul" : "ol", {});
      const listRe = unordered ? /^\s*[-*+]\s+(.+)$/ : /^\s*\d+\.\s+(.+)$/;
      while (index < lines.length) {
        const item = lines[index].match(listRe);
        if (!item) break;
        const li = node("li", {});
        li.innerHTML = renderInlineMarkdown(item[1]);
        list.append(li);
        index += 1;
      }
      parent.append(list);
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      const quotes = [];
      while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
        quotes.push(lines[index].replace(/^\s*>\s?/, ""));
        index += 1;
      }
      const quote = node("blockquote", {});
      appendMarkdownLines(quote, quotes.join("\n"));
      parent.append(quote);
      continue;
    }

    const paragraph = [line];
    index += 1;
    while (index < lines.length && lines[index].trim() && !markdownSpecialLine(lines[index])) {
      paragraph.push(lines[index]);
      index += 1;
    }
    const p = node("p", {});
    p.innerHTML = renderInlineMarkdown(paragraph.join("\n")).replaceAll("\n", "<br>");
    parent.append(p);
  }
}

function markdownBlock(text, label = "Markdown") {
  const raw = String(text ?? "");
  const body = node("div", { className: "markdown-rendered" });
  const fenceRe = /```([^\n`]*)\n([\s\S]*?)```/g;
  let last = 0;
  for (const match of raw.matchAll(fenceRe)) {
    appendMarkdownLines(body, raw.slice(last, match.index));
    body.append(codeBlock(match[2], match[1].trim() || "text", match[1].trim() || "code", { collapsed: match[2].length > BIG_CODE_CHARS }));
    last = match.index + match[0].length;
  }
  appendMarkdownLines(body, raw.slice(last));
  return node(
    "div",
    { className: "markdown-frame" },
    node("div", { className: "markdown-header" }, node("span", { text: label }), copyButton("Copy raw", raw)),
    body
  );
}

function looksMarkdownLike(text) {
  return /```|^#{1,6}\s+|^\s*[-*+]\s+|^\s*\d+\.\s+|^\s*>\s?|\|.+\||\[[^\]]+\]\([^)]+\)|\*\*[^*]+\*\*|`[^`]+`/m.test(String(text || ""));
}

function guessLanguageFromText(text) {
  const trimmed = String(text || "").trim();
  if (!trimmed) return "text";
  if (/^```([^\n`]*)/.test(trimmed)) return RegExp.$1.trim() || "text";
  if (/^(<!doctype|<html\b|<\?xml|<svg\b)/i.test(trimmed)) return "html";
  if (/^[{[]/.test(trimmed)) {
    try {
      JSON.parse(trimmed);
      return "json";
    } catch {
      return "javascript";
    }
  }
  if (/^(import|export|const|let|var|function|class)\s/m.test(trimmed)) return "javascript";
  if (/^(def|from|import|class)\s/m.test(trimmed) && /:\s*(#.*)?$/m.test(trimmed)) return "python";
  if (/^#!/.test(trimmed) || /\b(bash|zsh|sh)\b/.test(trimmed.slice(0, 120))) return "shell";
  if (/^#\s|\n#{1,6}\s/.test(trimmed)) return "markdown";
  return "text";
}

function findCodeStart(text) {
  const match = String(text || "").match(/(^|\n)\s*(```|<!doctype|<html\b|<\?xml|<svg\b|[{[]\s*(["{\[]|$)|(?:import|export|const|let|var|function|class)\s|(?:def|from)\s|#!)/i);
  if (!match) return -1;
  return match.index + (match[0].startsWith("\n") ? 1 : 0);
}

function looksCodeLike(text) {
  const raw = String(text || "");
  const trimmed = raw.trim();
  if (!trimmed) return false;
  if (findCodeStart(trimmed) === 0) return true;
  if (/^[-\w\s.#:[\]>~'"(),]+{\s*$/m.test(raw) && /;\s*$/m.test(raw)) return true;
  if ((raw.match(/<\/?[a-z][\w:-]*[\s>]/gi) || []).length >= 8) return true;
  if ((raw.match(/[{};]/g) || []).length >= 30 && raw.split("\n").length >= 8) return true;
  return false;
}

function nonEmptyLines(text) {
  return String(text || "")
    .split(/\r?\n/)
    .map((line) => line.trimEnd())
    .filter((line) => line.trim());
}

function countMatching(lines, re) {
  return lines.reduce((count, line) => count + (re.test(line) ? 1 : 0), 0);
}

function looksPathList(text) {
  const lines = nonEmptyLines(text);
  if (lines.length < 8) return false;
  const pathLines = countMatching(lines, /^\s*(?:[-*]\s+)?(?:\/mnt\/|\/home\/|\/tmp\/|\/usr\/|\.{1,2}\/|[A-Za-z]:\\)[^\s]+/);
  return pathLines >= 8 && pathLines / lines.length >= 0.55;
}

function looksDirectoryListing(text) {
  const lines = nonEmptyLines(text);
  if (lines.length < 6) return false;
  const listingLines = countMatching(lines, /^(?:total\s+\d+|[bcdlps-][rwxStTs-]{9}\s+\d+\s+\S+\s+\S+\s+\d+|\/[^:\n]+:\s*$)/);
  return listingLines >= 4;
}

function looksLayoutProbe(text) {
  const lines = nonEmptyLines(text);
  if (lines.length < 8) return false;
  return countMatching(lines, /^WIDTH\s+\d+\s+cw\s+[\d.]+\s+indent\s+/) >= 3;
}

function looksTraceback(text) {
  return /(^|\n)(Traceback \(most recent call last\):|[-]{5,}\n(?:\w+Error|KeyboardInterrupt)\b|Command '.+' returned non-zero exit status|\bError:|\bException:)/.test(
    String(text || "")
  );
}

function looksResourceYaml(text) {
  return /^---\s*\nname:\s+[\w.-]+\ndescription:/m.test(String(text || ""));
}

function looksGrepOutput(text) {
  const lines = nonEmptyLines(text);
  if (lines.length < 8) return false;
  const grepLines = countMatching(lines, /^(?:\/[^:\n]+|[\w./-]+):\d+:/);
  return grepLines >= 8 && grepLines / lines.length >= 0.45;
}

function isToolContext(context = {}) {
  const message = context.message || {};
  return message.author?.role === "tool" || Boolean(message.author?.name) || context.contentType === "execution_output";
}

function technicalPayloadKind(text, context = {}) {
  const raw = String(text || "");
  const trimmed = raw.trim();
  if (!trimmed) return null;
  if (looksLayoutProbe(raw)) return "layout probe output";
  if (looksPathList(raw)) return "file list";
  if (looksDirectoryListing(raw)) return "directory listing";
  if (looksTraceback(raw)) return "error output";
  if (looksResourceYaml(raw)) return "resource payload";
  if (looksGrepOutput(raw)) return "search/listing output";
  if (/^The latest state of the widget is:\s*{/.test(trimmed)) return "widget state";
  if (/^The file contents provided above are truncated\/partial snippets\./.test(trimmed)) return "file-search note";
  if (/^All the files uploaded by the user have been fully loaded\./.test(trimmed)) return "file-search note";
  if (/^Generated images from the last `image_gen\.text2im` call were saved at:/.test(trimmed)) return "generated image paths";
  if (context.contentType === "execution_output") return `${context.toolName || "tool"} output`;
  if (isToolContext(context) && raw.length > 900) return `${context.toolName || "tool"} output`;
  if (raw.length > BIG_CODE_CHARS && looksCodeLike(raw)) return "source/code payload";
  return null;
}

function splitInternalToolPayload(text) {
  const raw = String(text || "");
  const instructionMatch = raw.match(/Make sure to include[\s\S]{0,400}filecite[^\n]*(?:\n|$)/);
  if (!instructionMatch) return null;
  const instructionEnd = instructionMatch.index + instructionMatch[0].length;
  const payload = raw.slice(instructionEnd).trimStart();
  if (payload) {
    return {
      instruction: raw.slice(0, instructionEnd).trim(),
      payload,
    };
  }
  const codeStart = findCodeStart(raw);
  if (codeStart > 0) {
    return {
      instruction: raw.slice(0, codeStart).trim(),
      payload: raw.slice(codeStart).trimStart(),
    };
  }
  return { instruction: raw, payload: "" };
}

function collapsedPayloadBlock(text, label = "captured file/tool payload") {
  const raw = String(text ?? "");
  const body = looksMarkdownLike(raw) && !looksCodeLike(raw)
    ? markdownBlock(raw, label)
    : codeBlock(raw, guessLanguageFromText(raw), label, { collapsed: false });
  return node(
    "details",
    { className: "payload-details" },
    node(
      "summary",
      {},
      node("span", { text: label }),
      node("span", { className: "summary-meta", text: `${raw.length.toLocaleString()} chars · collapsed` })
    ),
    body
  );
}

function renderSmartText(text, context = {}) {
  const raw = String(text ?? "");
  const split = splitInternalToolPayload(raw);
  if (split) {
    const children = [
      node(
        "details",
        { className: "tool-note" },
        node("summary", { text: "Internal file-citation instruction" }),
        node("div", { className: "text-block details-body", text: split.instruction })
      ),
    ];
    if (split.payload) {
      children.push(collapsedPayloadBlock(split.payload));
    }
    return node("div", { className: "smart-stack" }, children);
  }

  const payloadKind = technicalPayloadKind(raw, context);
  if (payloadKind) {
    return collapsedPayloadBlock(raw, payloadKind);
  }

  if (looksMarkdownLike(raw)) {
    return markdownBlock(raw, context.label || "Markdown");
  }

  return node("div", { className: "text-block", text: raw });
}

function setHeader(title, subtitle, actions = []) {
  $("view-title").textContent = title;
  $("view-subtitle").textContent = subtitle || "";
  $("view-actions").replaceChildren(...actions);
}

function setContent(...children) {
  $("content").replaceChildren(...children);
}

function statusClass(ok) {
  if (ok === true) return "status-ok";
  if (ok === false) return "status-bad";
  return "status-warn";
}

function roleClass(role) {
  return `role ${role || "unknown"}`;
}

function makePill(label, active, onClick) {
  return node("button", { className: `pill${active ? " active" : ""}`, onClick, text: label });
}

function makeToggle(label, key) {
  return makePill(label, state[key], () => {
    state[key] = !state[key];
    render();
  });
}

function getMessageText(message) {
  if (!message) return "";
  const content = message.content || {};
  const parts = [];
  if (Array.isArray(content.parts)) {
    for (const part of content.parts) {
      if (typeof part === "string") parts.push(part);
      else if (part && typeof part === "object") {
        parts.push(part.text || part.asset_pointer || part.content_type || "");
      }
    }
  }
  if (content.text) parts.push(content.text);
  if (Array.isArray(content.thoughts)) {
    for (const thought of content.thoughts) {
      parts.push(thought.summary || "", thought.content || "");
      if (Array.isArray(thought.chunks)) parts.push(...thought.chunks);
    }
  }
  if (message.metadata) {
    for (const key of ["image_gen_title", "request_id", "model_slug", "resolved_model_slug"]) {
      if (message.metadata[key]) parts.push(message.metadata[key]);
    }
  }
  return parts.filter(Boolean).join("\n");
}

function getConversationText(conversation) {
  const body = conversation.body || {};
  const values = Object.values(body.mapping || {});
  return [
    body.title,
    body.conversation_id,
    ...values.map((entry) => getMessageText(entry.message)),
  ].join("\n").toLowerCase();
}

function buildMessageRows(conversation) {
  const mapping = conversation.body?.mapping || {};
  return Object.entries(mapping)
    .map(([id, entry], index) => ({ id, entry, index, message: entry.message }))
    .filter((row) => row.message)
    .sort((a, b) => {
      const at = a.message.create_time;
      const bt = b.message.create_time;
      if (typeof at === "number" && typeof bt === "number" && at !== bt) return at - bt;
      if (typeof at === "number" && typeof bt !== "number") return 1;
      if (typeof at !== "number" && typeof bt === "number") return -1;
      return a.index - b.index;
    });
}

function buildConversationGraph(conversation) {
  const mapping = conversation.body?.mapping || {};
  const rows = Object.entries(mapping).map(([id, entry], index) => ({ id, entry, index, message: entry.message }));
  const byId = new Map(rows.map((row) => [row.id, row]));
  const childrenByParent = new Map();
  for (const row of rows) {
    const parent = row.entry?.parent ?? null;
    if (!childrenByParent.has(parent)) childrenByParent.set(parent, []);
    childrenByParent.get(parent).push(row.id);
  }
  for (const ids of childrenByParent.values()) {
    ids.sort((a, b) => compareGraphRows(byId.get(a), byId.get(b)));
  }
  const messageRows = rows.filter((row) => row.message).sort(compareGraphRows);
  const branchCount = [...childrenByParent.values()].filter((ids) => ids.length > 1).length;
  return { byId, childrenByParent, rows, messageRows, branchCount };
}

function compareGraphRows(a, b) {
  const at = a?.message?.create_time;
  const bt = b?.message?.create_time;
  if (typeof at === "number" && typeof bt === "number" && at !== bt) return at - bt;
  if (typeof at === "number" && typeof bt !== "number") return 1;
  if (typeof at !== "number" && typeof bt === "number") return -1;
  return (a?.index || 0) - (b?.index || 0);
}

function conversationStateId(summary, conversation) {
  return conversation.body?.conversation_id || summary.id;
}

function choicesForConversation(id) {
  if (!state.branchChoices.has(id)) state.branchChoices.set(id, new Map());
  return state.branchChoices.get(id);
}

function initializeBranchChoices(summary, conversation, graph) {
  const id = conversationStateId(summary, conversation);
  const choices = choicesForConversation(id);
  if (choices.get("__initialized")) return choices;

  let cursor = conversation.body?.current_node;
  const path = [];
  while (cursor && graph.byId.has(cursor)) {
    path.push(cursor);
    cursor = graph.byId.get(cursor)?.entry?.parent;
  }
  path.reverse();
  for (const nodeId of path) {
    const parent = graph.byId.get(nodeId)?.entry?.parent ?? null;
    choices.set(parent, nodeId);
  }
  choices.set("__initialized", true);
  return choices;
}

function selectedChild(graph, choices, parentId) {
  const children = graph.childrenByParent.get(parentId ?? null) || [];
  if (!children.length) return null;
  const selected = choices.get(parentId ?? null);
  if (selected && children.includes(selected)) {
    const selectedRow = graph.byId.get(selected);
    const chatAlternative = children.find((id) => {
      const row = graph.byId.get(id);
      return row?.message && isChatVariantCandidate(row.message);
    });
    if (!state.showTechnical && chatAlternative && selectedRow?.message && !isChatVariantCandidate(selectedRow.message)) return chatAlternative;
    return selected;
  }
  return children[children.length - 1];
}

function buildSelectedRows(graph, choices) {
  const rows = [];
  const seen = new Set();
  let cursor = selectedChild(graph, choices, null);
  while (cursor && !seen.has(cursor)) {
    seen.add(cursor);
    const row = graph.byId.get(cursor);
    if (!row) break;
    if (row.message) rows.push(row);
    cursor = selectedChild(graph, choices, cursor);
  }
  return rows;
}

function isHiddenMessage(message) {
  return Boolean(message?.metadata?.is_visually_hidden_from_conversation);
}

function isEmptySystem(message) {
  return message?.author?.role === "system" && !getMessageText(message).trim();
}

function isTechnicalMessage(message) {
  if (!message) return true;
  const content = message.content || {};
  const role = message.author?.role;
  if (role === "tool") return true;
  if (message.recipient && message.recipient !== "all") return true;
  if (["thoughts", "reasoning_recap", "model_editable_context", "execution_output"].includes(content.content_type)) return true;
  if (message.metadata?.reasoning_status === "is_reasoning" && content.content_type !== "text") return true;
  return false;
}

function isChatVariantCandidate(message) {
  if (!message) return false;
  const role = message.author?.role;
  return (role === "user" || role === "assistant") && !isTechnicalMessage(message) && !isNoopMessage(message);
}

function isNoopMessage(message) {
  const content = message?.content || {};
  const text = getMessageText(message).trim();
  if (!text) return true;
  if (content.content_type === "tether_browsing_display") return true;
  if (/^(null|\[\]|""|<<ImageDisplayed>>)$/i.test(text)) return true;
  if (message?.author?.role === "tool" && /^The tool included embedded UI which has been displayed to the user\./.test(text)) return true;
  return false;
}

function passesSystemHiddenFilters(message) {
  if (!state.showHidden && isHiddenMessage(message)) return false;
  if (!state.showSystem && (message.author?.role === "system" || isEmptySystem(message))) return false;
  return true;
}

function technicalLabel(message) {
  const content = message.content || {};
  const toolName = message.author?.name || message.recipient || "tool";
  if (message.recipient && message.recipient !== "all") return `${message.recipient} call`;
  if (content.content_type === "execution_output") return `${toolName} output`;
  if (content.content_type === "thoughts") return "reasoning";
  if (content.content_type === "reasoning_recap") return "reasoning recap";
  if (content.content_type === "model_editable_context") return "model context";
  if (message.author?.role === "tool") return `${toolName} ${content.content_type || "event"}`;
  return content.content_type || "technical event";
}

function messageTitle(message) {
  const role = message.author?.role || "unknown";
  if (role === "user") return "You";
  if (role === "assistant") return message.author?.name || "ChatGPT";
  if (role === "tool") return technicalLabel(message);
  if (role === "system") return "System";
  return message.author?.name || role;
}

function visibleMessageRows(rows, query) {
  return rows.filter(({ message }) => {
    if (!passesSystemHiddenFilters(message)) return false;
    if (!state.showTechnical && !query && isTechnicalMessage(message)) return false;
    if (!state.showTechnical && !query && isNoopMessage(message)) return false;
    if (!query) return true;
    return [message.id, message.author?.role, message.author?.name, message.recipient, getMessageText(message), JSON.stringify(message.metadata || {})]
      .join("\n")
      .toLowerCase()
      .includes(query);
  });
}

function countHiddenTechnical(rows, query) {
  if (state.showTechnical || query) return 0;
  return rows.filter(({ message }) => passesSystemHiddenFilters(message) && (isTechnicalMessage(message) || isNoopMessage(message))).length;
}

function extractFileIds(value) {
  const ids = new Set();
  const text = typeof value === "string" ? value : JSON.stringify(value || "");
  for (const match of text.matchAll(FILE_ID_RE)) ids.add(match[0]);
  return [...ids];
}

function fileRowForId(fileId) {
  return data.fileById.get(fileId);
}

function fileName(row) {
  if (!row) return "";
  const library = data.libraryByFileId.get(row.file_id);
  if (library?.name) return library.name;
  const rel = relativePath(row.path);
  return rel ? rel.split("/").pop() : row.file_id;
}

function fileHref(row) {
  return row?.path ? hrefForPath(row.path) : null;
}

function isImageFile(row) {
  const type = row?.headers?.["content-type"] || row?.mime_type || "";
  const name = fileName(row).toLowerCase();
  return type.startsWith("image/") || /\.(png|jpe?g|webp|gif|svg)$/.test(name);
}

function isPdfFile(row) {
  const type = row?.headers?.["content-type"] || "";
  return type.includes("pdf") || /\.pdf$/i.test(fileName(row));
}

function isTextFile(row) {
  const name = fileName(row).toLowerCase();
  const type = row?.headers?.["content-type"] || "";
  return type.startsWith("text/") || /\.(txt|md|py|js|css|json|html|xml|csv|sh|toml|yaml|yml)$/i.test(name);
}

function renderFileCard(fileId, label = null) {
  const row = fileRowForId(fileId);
  const href = fileHref(row);
  const title = label || fileName(row) || fileId;
  const card = node("div", { className: "asset-card" });
  if (row?.ok && href && isImageFile(row)) {
    card.append(node("a", { href, target: "_blank" }, node("img", { src: href, alt: title })));
  } else {
    card.append(node("div", { className: row?.ok ? "status-ok" : "status-bad", text: row?.ok ? "Downloaded" : "Missing" }));
  }
  card.append(node("div", { className: "asset-name", title, text: title }));
  card.append(node("div", { className: "asset-name", text: fileId }));
  if (href) card.append(node("a", { href, target: "_blank", text: "Open file" }));
  return card;
}

function isFileCitationInstructionText(text) {
  return /Make sure to include[\s\S]{0,400}filecite/.test(String(text || ""));
}

function renderToolFileSearchParts(parts) {
  const instructions = [];
  const textPayload = [];
  const assetParts = [];
  for (const part of parts) {
    if (typeof part === "string") {
      if (isFileCitationInstructionText(part)) instructions.push(part.trim());
      else textPayload.push(part);
    } else if (part?.content_type === "image_asset_pointer") {
      assetParts.push(part);
    } else if (part) {
      textPayload.push(JSON.stringify(part, null, 2));
    }
  }

  const children = [];
  if (instructions.length) {
    children.push(
      node(
        "details",
        { className: "tool-note" },
        node("summary", { text: "Internal file-citation instruction" }),
        node("div", { className: "text-block details-body", text: instructions.join("\n\n") })
      )
    );
  }

  const payload = textPayload.join("\n").trim();
  if (payload) children.push(collapsedPayloadBlock(payload, "captured file-search output"));

  if (assetParts.length) {
    const cards = assetParts.flatMap((part) => {
      const ids = extractFileIds(part.asset_pointer);
      return ids.length ? ids.map((id) => renderFileCard(id, part.asset_pointer)) : [detailsBlock("Image asset pointer", part)];
    });
    children.push(
      node(
        "details",
        { className: "asset-details" },
        node("summary", { text: `Referenced page images (${assetParts.length})` }),
        node("div", { className: "asset-grid details-body" }, cards)
      )
    );
  }

  return children.length ? children : [detailsBlock("Tool output", parts)];
}

function renderContentPart(part, message = null) {
  const context = {
    message,
    toolName: message?.author?.name || message?.recipient || "tool",
    contentType: message?.content?.content_type,
  };
  if (typeof part === "string") return renderSmartText(part, { ...context, label: "Markdown" });
  if (!part || typeof part !== "object") return renderSmartText(String(part), { ...context, label: "Text" });
  if (part.content_type === "image_asset_pointer") {
    const fileIds = extractFileIds(part.asset_pointer);
    return node(
      "div",
      { className: "asset-grid" },
      fileIds.length ? fileIds.map((id) => renderFileCard(id, part.asset_pointer)) : detailsBlock("Image asset pointer", part)
    );
  }
  if (part.text) return renderSmartText(part.text, { ...context, contentType: part.content_type || context.contentType, label: part.content_type || "Markdown" });
  return detailsBlock(part.content_type || "Content part", part);
}

function renderMessageContent(message) {
  const content = message.content || {};
  const children = [];
  const toolName = message.author?.name || message.recipient || "tool";

  if (content.content_type === "thoughts" && Array.isArray(content.thoughts)) {
    for (const thought of content.thoughts) {
      children.push(
        node(
          "details",
          { className: "thought-details" },
          node("summary", { text: thought.summary || "Reasoning" }),
          node(
            "div",
            { className: "thought-block" },
            thought.summary ? node("div", { className: "thought-summary", text: thought.summary }) : null,
            thought.content ? renderSmartText(thought.content, { message, contentType: "thoughts", label: "Reasoning" }) : null
          ),
          Array.isArray(thought.chunks) && thought.chunks.length ? detailsBlock("Chunks", thought.chunks) : null
        )
      );
    }
  } else if (content.content_type === "code") {
    const isToolCall = Boolean(message.recipient && message.recipient !== "all");
    children.push(
      codeBlock(
        content.text || "",
        content.language || "text",
        isToolCall ? `tool call: ${message.recipient}` : content.language || "code",
        { collapsed: isToolCall || String(content.text || "").length > BIG_CODE_CHARS }
      )
    );
  } else if (content.content_type === "execution_output") {
    children.push(renderSmartText(content.text || "", { message, contentType: "execution_output", toolName, label: `${toolName} output` }));
  } else if (Array.isArray(content.parts)) {
    if (message.author?.role === "tool" && content.parts.some((part) => typeof part === "string" && isFileCitationInstructionText(part))) {
      children.push(...renderToolFileSearchParts(content.parts));
    } else {
      for (const part of content.parts) children.push(renderContentPart(part, message));
    }
  } else if (content.text) {
    children.push(renderSmartText(content.text, { message, contentType: content.content_type, toolName, label: content.content_type || "Markdown" }));
  } else {
    children.push(detailsBlock(content.content_type || "Content", content, false));
  }

  const metadata = message.metadata || {};
  const attachments = metadata.attachments || [];
  if (attachments.length) {
    children.push(
      node(
        "div",
        { className: "asset-grid" },
        attachments.flatMap((attachment) => {
          const ids = extractFileIds(attachment.id || attachment.file_id || attachment);
          return ids.length ? ids.map((id) => renderFileCard(id, attachment.name || id)) : [detailsBlock("Attachment", attachment)];
        })
      )
    );
  }

  if (metadata.citations?.length) children.push(detailsBlock(`Citations (${metadata.citations.length})`, metadata.citations));
  if (metadata.content_references?.length) {
    children.push(detailsBlock(`Content references (${metadata.content_references.length})`, metadata.content_references));
  }
  if (metadata.aggregate_result) children.push(detailsBlock("Aggregate result", metadata.aggregate_result));
  if (state.showRaw) children.push(detailsBlock("Raw message", message));

  return children;
}

function renderVariantControls(row, graph, conversationId) {
  const parent = row.entry?.parent ?? null;
  if (!isChatVariantCandidate(row.message)) return null;
  const allSiblings = graph.childrenByParent.get(parent) || [];
  const role = row.message?.author?.role || "node";
  const siblings = allSiblings.filter((id) => {
    const candidate = graph.byId.get(id)?.message;
    return candidate && candidate.author?.role === role && isChatVariantCandidate(candidate);
  });
  if (siblings.length < 2) return null;
  const index = siblings.indexOf(row.id);
  if (index < 0) return null;
  const choices = choicesForConversation(conversationId);
  const selectSibling = (nextIndex) => {
    const nextId = siblings[nextIndex];
    if (!nextId) return;
    choices.set(parent, nextId);
    render();
  };
  return node(
    "div",
    { className: "variant-nav" },
    node("button", {
      className: "variant-button",
      type: "button",
      title: `Previous ${role} variant`,
      text: "‹",
      disabled: index === 0,
      onClick: () => selectSibling(index - 1),
    }),
    node("span", { className: "variant-count", text: `${index + 1} / ${siblings.length}` }),
    node("button", {
      className: "variant-button",
      type: "button",
      title: `Next ${role} variant`,
      text: "›",
      disabled: index === siblings.length - 1,
      onClick: () => selectSibling(index + 1),
    })
  );
}

function renderMessage(row, graph = null, conversationId = null) {
  const message = row.message;
  const role = message.author?.role || "unknown";
  const technical = isTechnicalMessage(message) || isNoopMessage(message);
  const textLength = getMessageText(message).length;
  const detailBits = [textLength ? `${textLength.toLocaleString()} chars` : "empty"];

  if (technical) return renderTechnicalMessage(row, graph, conversationId, detailBits);

  return node(
    "article",
    { className: `message message-${role}${isHiddenMessage(message) ? " hidden-node" : ""}` },
    node(
      "div",
      { className: "message-header" },
      node(
        "div",
        { className: "message-heading" },
        node("span", { className: roleClass(role), text: role }),
        node("span", { className: "message-title", text: messageTitle(message) })
      ),
      node("div", { className: "message-meta" }, formatDate(message.create_time))
    ),
    node("div", { className: "message-body" }, renderMessageContent(message)),
    graph && conversationId ? renderVariantControls(row, graph, conversationId) : null
  );
}

function renderTechnicalMessage(row, graph = null, conversationId = null, detailBits = []) {
  const message = row.message;
  const role = message.author?.role || "unknown";
  const summary = [
    node("span", { className: "technical-title", text: technicalLabel(message) }),
    detailBits.length ? node("span", { className: "technical-bits", text: detailBits.join(" · ") }) : null,
    node("span", { className: "technical-time", text: formatDate(message.create_time) }),
  ];
  return node(
    "article",
    { className: `message message-technical message-${role}${isHiddenMessage(message) ? " hidden-node" : ""}` },
    node(
      "details",
      { className: "technical-details" },
      node("summary", {}, summary),
      node("div", { className: "message-body technical-body" }, renderMessageContent(message), graph && conversationId ? renderVariantControls(row, graph, conversationId) : null)
    )
  );
}

function renderConversationList() {
  const query = state.query.trim().toLowerCase();
  const rows = data.conversations.filter((conversation) => {
    if (!query) return true;
    const loaded = data.conversationBodies.get(conversation.id);
    const text = loaded?._searchText || [conversation.title, conversation.id].join("\n").toLowerCase();
    return text.includes(query);
  });

  $("conversation-list").replaceChildren(
    ...rows.map((conversation) =>
      node(
        "button",
        {
          className: `conversation-button${conversation.id === state.selectedConversationId ? " active" : ""}`,
          onClick: () => {
            state.view = "conversations";
            state.selectedConversationId = conversation.id;
            updateNav();
            render();
          },
        },
        node("div", { className: "conversation-title", text: conversation.title || conversation.id }),
        node("div", { className: "conversation-meta", text: `${conversation.message_nodes || 0} nodes · ${formatDate(conversation.update_time)}` })
      )
    )
  );
}

function updateNav() {
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === state.view);
  });
  renderConversationList();
}

function renderOverview() {
  const counts = data.manifest.counts || {};
  setHeader("Overview", `${data.manifest.account?.planType || "workspace"} export · ${formatBytes(totalDownloadedBytes())}`, []);

  const stats = [
    ["Conversations", counts.conversation_summaries],
    ["Library nodes", counts.library_nodes],
    ["Files downloaded", counts.files_downloaded],
    ["Artifact refs", counts.artifact_references],
    ["Endpoint snapshots", counts.endpoint_snapshots],
    ["Total dump size", formatBytes(totalDownloadedBytes())],
  ];

  const recent = data.conversations.slice(0, 8).map((conversation) =>
    node(
      "tr",
      {},
      node("td", {}, node("button", { className: "row-button", text: conversation.title || conversation.id, onClick: () => openConversation(conversation.id) })),
      node("td", { text: String(conversation.message_nodes || 0) }),
      node("td", { text: formatDate(conversation.update_time) })
    )
  );

  const fileTypes = countFileTypes();

  setContent(
    node("div", { className: "grid stats-grid" }, stats.map(([label, value]) => node("div", { className: "panel stat" }, node("div", { className: "stat-value", text: value ?? 0 }), node("div", { className: "stat-label", text: label })))),
    node(
      "div",
      { className: "two-col", style: "margin-top:16px" },
      node(
        "section",
        { className: "panel" },
        node("div", { className: "panel-header" }, node("div", { className: "panel-title", text: "Recent conversations" })),
        node("div", { className: "table-wrap" }, node("table", {}, node("thead", {}, node("tr", {}, node("th", { text: "Title" }), node("th", { text: "Nodes" }), node("th", { text: "Updated" }))), node("tbody", {}, recent)))
      ),
      node(
        "section",
        { className: "panel" },
        node("div", { className: "panel-header" }, node("div", { className: "panel-title", text: "Downloaded file types" })),
        node(
          "div",
          { className: "panel-body" },
          node(
            "div",
            { className: "pill-row" },
            [...fileTypes.entries()].slice(0, 18).map(([type, count]) => node("span", { className: "pill", text: `${type} ${count}` }))
          )
        )
      )
    )
  );
}

function openConversation(id) {
  state.view = "conversations";
  state.selectedConversationId = id;
  updateNav();
  render();
}

function renderConversations() {
  if (!state.selectedConversationId && data.conversations[0]) state.selectedConversationId = data.conversations[0].id;
  const summary = data.conversations.find((conversation) => conversation.id === state.selectedConversationId);
  const conversation = summary ? data.conversationBodies.get(summary.id) : null;
  if (!summary || !conversation) {
    setHeader("Conversations", "No conversation selected");
    setContent(node("div", { className: "empty", text: "No conversation loaded." }));
    return;
  }

  const graph = buildConversationGraph(conversation);
  const conversationId = conversationStateId(summary, conversation);
  const choices = initializeBranchChoices(summary, conversation, graph);
  const rows = graph.messageRows;
  const selectedRows = buildSelectedRows(graph, choices);
  const query = state.query.trim().toLowerCase();
  const baseRows = state.showAllNodes || query ? rows : selectedRows;
  const filtered = visibleMessageRows(baseRows, query);
  const shown = filtered.slice(0, state.conversationLimit);
  const hiddenTechnical = countHiddenTechnical(baseRows, query);

  setHeader(
    conversation.body?.title || summary.title || "Conversation",
    `${shown.length} shown of ${filtered.length} ${state.showAllNodes || query ? "matching nodes" : "chat messages"} · ${hiddenTechnical ? `${hiddenTechnical} technical hidden · ` : ""}${rows.length} total nodes · ${graph.branchCount} branch points`,
    [
      makeToggle("All nodes", "showAllNodes"),
      makeToggle("Technical", "showTechnical"),
      makeToggle("Hidden", "showHidden"),
      makeToggle("System", "showSystem"),
      makeToggle("Raw", "showRaw"),
      node("a", { className: "small-button", href: `../${summary.path}`, target: "_blank", text: "Raw JSON" }),
    ]
  );

  const inspector = node(
    "aside",
    { className: "inspector" },
    node(
      "section",
      { className: "panel" },
      node("div", { className: "panel-header" }, node("div", { className: "panel-title", text: "Conversation" })),
      node(
        "div",
        { className: "panel-body" },
        kv("ID", summary.id),
        kv("Created", formatDate(summary.create_time)),
        kv("Updated", formatDate(summary.update_time)),
        kv("Current node", conversation.body?.current_node),
        kv("Model", conversation.body?.default_model_slug),
        kv("Archived", String(summary.is_archived)),
        kv("Starred", String(summary.is_starred))
      )
    ),
    node(
      "section",
      { className: "panel" },
      node("div", { className: "panel-header" }, node("div", { className: "panel-title", text: "Node counts" })),
      node("div", { className: "panel-body" }, renderRoleCounts(rows))
    )
  );

  const messageList = node(
    "div",
    { className: "messages" },
    shown.map((row) => renderMessage(row, graph, conversationId)),
    filtered.length > shown.length
      ? node("button", {
          className: "small-button",
          text: `Show ${Math.min(500, filtered.length - shown.length)} more`,
          onClick: () => {
            state.conversationLimit += 500;
            render();
          },
        })
      : null
  );

  setContent(node("div", { className: "conversation-view" }, messageList, inspector));
}

function kv(key, value) {
  return node("div", { className: "kv" }, node("div", { className: "kv-key", text: key }), node("div", { className: "kv-value", text: value || "" }));
}

function renderRoleCounts(rows) {
  const counts = new Map();
  for (const { message } of rows) counts.set(message.author?.role || "unknown", (counts.get(message.author?.role || "unknown") || 0) + 1);
  return node("div", { className: "pill-row" }, [...counts.entries()].map(([role, count]) => node("span", { className: roleClass(role), text: `${role} ${count}` })));
}

function renderFiles() {
  const rows = filteredFileRows();
  if (!state.selectedFileId && rows[0]) state.selectedFileId = rows[0].file_id;
  const selected = data.fileById.get(state.selectedFileId) || rows[0];

  setHeader(
    "Files",
    `${rows.length} matching files · ${data.fileDownloads.filter((row) => row.ok).length} downloaded`,
    [
      makePill("All", state.fileKind === "all", () => setFileKind("all")),
      makePill("Images", state.fileKind === "images", () => setFileKind("images")),
      makePill("PDF/Text", state.fileKind === "docs", () => setFileKind("docs")),
      makePill("Failed", state.fileKind === "failed", () => setFileKind("failed")),
    ]
  );

  const table = node(
    "div",
    { className: "table-wrap" },
    node(
      "table",
      {},
      node("thead", {}, node("tr", {}, node("th", { text: "File" }), node("th", { text: "Type" }), node("th", { text: "Size" }), node("th", { text: "Status" }))),
      node(
        "tbody",
        {},
        rows.slice(0, 450).map((row) =>
          node(
            "tr",
            {},
            node("td", {}, node("button", { className: "row-button", text: fileName(row) || row.file_id, onClick: () => selectFile(row.file_id) }), node("div", { className: "conversation-meta", text: row.file_id })),
            node("td", { text: row.headers?.["content-type"] || data.libraryByFileId.get(row.file_id)?.mime_type || "" }),
            node("td", { text: formatBytes(row.bytes || data.libraryByFileId.get(row.file_id)?.file_size_bytes) }),
            node("td", { className: statusClass(row.ok), text: row.ok ? "downloaded" : "missing" })
          )
        )
      )
    )
  );

  setContent(node("div", { className: "two-col" }, table, renderFilePreview(selected)));
}

function setFileKind(kind) {
  state.fileKind = kind;
  state.selectedFileId = null;
  render();
}

function selectFile(fileId) {
  state.selectedFileId = fileId;
  render();
}

function filteredFileRows() {
  const query = state.query.trim().toLowerCase();
  return data.fileDownloads.filter((row) => {
    if (state.fileKind === "images" && !isImageFile(row)) return false;
    if (state.fileKind === "docs" && !(isPdfFile(row) || isTextFile(row))) return false;
    if (state.fileKind === "failed" && row.ok) return false;
    if (!query) return true;
    const library = data.libraryByFileId.get(row.file_id);
    return [row.file_id, fileName(row), row.headers?.["content-type"], library?.name, library?.mime_type].join("\n").toLowerCase().includes(query);
  });
}

function renderFilePreview(row) {
  if (!row) return node("section", { className: "panel" }, node("div", { className: "panel-body", text: "No file selected." }));
  const href = fileHref(row);
  const library = data.libraryByFileId.get(row.file_id);
  const children = [
    node("div", { className: "panel-header" }, node("div", { className: "panel-title", text: fileName(row) || row.file_id }), href ? node("a", { className: "small-button", href, target: "_blank", text: "Open" }) : null),
    node("div", { className: "panel-body file-preview" }, kv("File ID", row.file_id), kv("Bytes", formatBytes(row.bytes || library?.file_size_bytes)), kv("MIME", row.headers?.["content-type"] || library?.mime_type), kv("SHA-256", row.sha256 || ""), renderInlinePreview(row), detailsBlock("Download record", row)),
  ];
  return node("section", { className: "panel" }, children);
}

function renderInlinePreview(row) {
  const href = fileHref(row);
  if (!row.ok || !href) return node("div", { className: "status-bad", text: row.error || "No local file." });
  if (isImageFile(row)) return node("a", { href, target: "_blank" }, node("img", { src: href, alt: fileName(row) }));
  if (isPdfFile(row)) return node("iframe", { src: href, title: fileName(row), attrs: { sandbox: "" } });
  if (/\.html?$/i.test(fileName(row))) return node("iframe", { src: href, title: fileName(row), attrs: { sandbox: "" } });
  if (isTextFile(row)) {
    const lang = languageFromName(fileName(row));
    const box = node("div", { className: "smart-stack" }, codeBlock("Loading text preview...", lang, lang, { collapsed: false }));
    fetch(href)
      .then((response) => response.text())
      .then((text) => {
        const preview = text.length > 180000 ? `${text.slice(0, 180000)}\n... truncated ${text.length - 180000} chars` : text;
        box.replaceChildren(codeBlock(preview, lang, lang, { collapsed: preview.length > BIG_CODE_CHARS }));
      })
      .catch((error) => {
        box.replaceChildren(codeBlock(String(error), "text", "error", { collapsed: false }));
      });
    return box;
  }
  return node("div", { className: "muted", text: "Preview unavailable for this file type." });
}

function renderLibrary() {
  const query = state.query.trim().toLowerCase();
  const rows = data.libraryNodes.filter((item) => !query || [item.name, item.file_id, item.mime_type, item.library_file_category, item.origination_thread_id].join("\n").toLowerCase().includes(query));
  setHeader("Library", `${rows.length} matching nodes · ${data.libraryNodes.length} total`);
  setContent(
    node(
      "div",
      { className: "table-wrap" },
      node(
        "table",
        {},
        node("thead", {}, node("tr", {}, node("th", { text: "Name" }), node("th", { text: "Category" }), node("th", { text: "Size" }), node("th", { text: "Conversation" }), node("th", { text: "Local" }))),
        node(
          "tbody",
          {},
          rows.map((item) => {
            const file = data.fileById.get(item.file_id);
            const conversation = data.conversations.find((entry) => entry.id === item.origination_thread_id);
            return node(
              "tr",
              {},
              node("td", {}, node("button", { className: "row-button", text: item.name || item.file_id, onClick: () => selectFile(item.file_id) }), node("div", { className: "conversation-meta", text: item.file_id || "" })),
              node("td", { text: item.library_file_category || item.mime_type || "" }),
              node("td", { text: formatBytes(item.file_size_bytes) }),
              node("td", {}, conversation ? node("button", { className: "row-button", text: conversation.title, onClick: () => openConversation(conversation.id) }) : node("span", { text: item.origination_thread_id || "" })),
              node("td", {}, file?.ok ? node("a", { href: fileHref(file), target: "_blank", text: "Open" }) : node("span", { className: "status-bad", text: "Missing" }))
            );
          })
        )
      )
    )
  );
}

function renderArtifacts() {
  const query = state.query.trim().toLowerCase();
  const rows = data.artifactRefs.filter((ref) => !query || [ref.kind, ref.value, JSON.stringify(ref.sources || [])].join("\n").toLowerCase().includes(query));
  setHeader("Artifacts", `${rows.length} matching references · ${data.artifactRefs.length} total`);
  setContent(
    node(
      "div",
      { className: "table-wrap" },
      node(
        "table",
        {},
        node("thead", {}, node("tr", {}, node("th", { text: "Kind" }), node("th", { text: "Value" }), node("th", { text: "Sources" }), node("th", { text: "Local" }))),
        node(
          "tbody",
          {},
          rows.slice(0, 1200).map((ref) => {
            const file = ref.kind === "file_id" ? data.fileById.get(ref.value) : null;
            return node(
              "tr",
              {},
              node("td", { text: ref.kind }),
              node("td", { text: ref.value }),
              node("td", { text: String(ref.sources?.length || 0) }),
              node("td", {}, file?.ok ? node("a", { href: fileHref(file), target: "_blank", text: "Open" }) : ref.kind === "file_id" ? node("span", { className: "status-bad", text: "Missing" }) : node("span", { text: "" }))
            );
          })
        )
      )
    )
  );
}

function renderEndpoints() {
  const query = state.query.trim().toLowerCase();
  const rows = data.endpoints.filter((endpoint) => !query || [endpoint.name, endpoint.url, endpoint.status].join("\n").toLowerCase().includes(query));
  if (!state.selectedEndpoint && rows[0]) state.selectedEndpoint = rows[0].name;
  const selected = data.endpoints.find((endpoint) => endpoint.name === state.selectedEndpoint) || rows[0];
  setHeader("Raw API", `${rows.length} matching snapshots`);

  const table = node(
    "div",
    { className: "table-wrap" },
    node(
      "table",
      {},
      node("thead", {}, node("tr", {}, node("th", { text: "Endpoint" }), node("th", { text: "Status" }))),
      node(
        "tbody",
        {},
        rows.map((endpoint) =>
          node(
            "tr",
            {},
            node("td", {}, node("button", { className: "row-button", text: endpoint.name, onClick: () => selectEndpoint(endpoint.name) }), node("div", { className: "conversation-meta", text: endpoint.url || "" })),
            node("td", { className: statusClass(endpoint.ok), text: String(endpoint.status) })
          )
        )
      )
    )
  );
  const preview = node("section", { className: "panel" }, node("div", { className: "panel-header" }, node("div", { className: "panel-title", text: selected?.name || "Endpoint" })), node("div", { className: "panel-body" }, codeBlock("Loading raw JSON...", "json", "json")));
  if (selected) {
    loadJson(`../${selected.path}`).then((json) => {
      preview.querySelector(".panel-body").replaceChildren(jsonBlock(json, 120000));
    });
  }
  setContent(node("div", { className: "two-col" }, table, preview));
}

function selectEndpoint(name) {
  state.selectedEndpoint = name;
  render();
}

function totalDownloadedBytes() {
  return data.fileDownloads.reduce((sum, row) => sum + (row.ok ? Number(row.bytes || 0) : 0), 0);
}

function countFileTypes() {
  const counts = new Map();
  for (const row of data.fileDownloads) {
    if (!row.ok) continue;
    const type = (row.headers?.["content-type"] || fileName(row).split(".").pop() || "unknown").split(";")[0];
    counts.set(type, (counts.get(type) || 0) + 1);
  }
  return new Map([...counts.entries()].sort((a, b) => b[1] - a[1]));
}

function render() {
  updateNav();
  if (state.view === "overview") renderOverview();
  else if (state.view === "conversations") renderConversations();
  else if (state.view === "files") renderFiles();
  else if (state.view === "library") renderLibrary();
  else if (state.view === "artifacts") renderArtifacts();
  else if (state.view === "endpoints") renderEndpoints();
}

async function init() {
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.addEventListener("click", () => {
      state.view = button.dataset.view;
      updateNav();
      render();
    });
  });
  $("global-search").addEventListener("input", (event) => {
    state.query = event.target.value;
    render();
  });

  data.manifest = await loadJson("../manifest.json");
  $("brand-subtitle").textContent = `${data.manifest.counts.conversation_summaries} conversations · ${data.manifest.counts.files_downloaded} files`;

  const [conversations, libraryNodes, fileDownloads, artifactRefs, endpoints, mediaDownloads, tasks] = await Promise.all([
    loadJson("../indexes/conversations.json"),
    loadJson("../indexes/library_nodes.json"),
    loadJson("../indexes/file_downloads.json"),
    loadJson("../indexes/artifact_references.json"),
    loadJson("../indexes/endpoint_snapshots.json"),
    loadJson("../indexes/media_downloads.json"),
    loadJson("../raw_api/tasks.json").catch(() => null),
  ]);

  data.conversations = conversations;
  data.libraryNodes = libraryNodes;
  data.fileDownloads = fileDownloads;
  data.artifactRefs = artifactRefs;
  data.endpoints = endpoints;
  data.mediaDownloads = mediaDownloads;
  data.tasks = tasks;
  data.selectedConversationId = conversations[0]?.id;

  for (const row of fileDownloads) data.fileById.set(row.file_id, row);
  for (const item of libraryNodes) if (item.file_id) data.libraryByFileId.set(item.file_id, item);
  for (const endpoint of endpoints) data.endpointByName.set(endpoint.name, endpoint);

  await loadAllConversations();
  state.selectedConversationId = conversations[0]?.id || null;
  render();
}

async function loadAllConversations() {
  $("view-subtitle").textContent = "Loading conversation payloads...";
  const entries = await Promise.all(
    data.conversations.map(async (summary) => {
      const json = await loadJson(`../${summary.path}`);
      json._searchText = getConversationText(json);
      return [summary.id, json];
    })
  );
  data.conversationBodies = new Map(entries);
}

init().catch((error) => {
  console.error(error);
  setHeader("Load failed", "The dump browser could not load one of the local JSON files.");
  setContent(codeBlock(String(error.stack || error), "text", "error"));
});
