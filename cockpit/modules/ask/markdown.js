// Parse with the same local Lute build as the editor. Render only known Markdown
// nodes: neither model HTML nor the parser's HTML output is inserted into the page.
const LUTE_SRC = "/vendor/vditor/dist/js/lute/lute.min.js";
let luteReady;

function loadLute(ctx){
  if (globalThis.Lute) return globalThis.Lute;
  if (!globalThis.document?.head) return null;
  return luteReady ||= new Promise(resolve => {
    const script = ctx.el("script", {src: LUTE_SRC});
    script.onload = () => resolve(globalThis.Lute);
    script.onerror = () => { script.remove(); luteReady = null; resolve(null); };
    document.head.append(script);
  });
}

const TAGS = new Map(Object.entries({
  NodeParagraph: "p", NodeStrong: "strong", NodeEmphasis: "em", NodeStrikethrough: "del",
  NodeListItem: "li", NodeBlockquote: "blockquote", NodeHardBreak: "br", NodeThematicBreak: "hr",
}));
const TEXT = new Set(["NodeInlineHTML", "NodeHTMLBlock", "NodeHTMLEntity", "NodeLinkText", "NodeBackslashContent"]);

function withCards(ctx, text){
  const parts = [];
  let from = 0;
  for (const link of text.matchAll(/\[\[([^\]|\n]+)(?:\|([^\]\n]+))?\]\]/g)){
    const [whole, key, caption] = link;
    parts.push(text.slice(from, link.index),
      ctx.el("span", {class:"chip", title: `${ctx.t("mdlite.card")}: ${key}`}, caption || key));
    from = link.index + whole.length;
  }
  return [...parts, text.slice(from)];
}

function safeLink(dest){
  if (!/^https?:\/\//i.test(dest || "")) return null;
  try {
    const url = new URL(dest);
    return url.username || url.password ? null : url.href;
  } catch { return null; }
}

function content(node, kind){
  return node.Children?.find(child => child.Type === kind)?.Data || "";
}

function renderNode(ctx, node){
  const el = ctx.el;
  const children = () => (node.Children || []).flatMap(child => renderNode(ctx, child));
  if (TAGS.has(node.Type)) return el(TAGS.get(node.Type), {}, children());
  if (TEXT.has(node.Type)) return node.Data || "";
  switch (node.Type){
  case "NodeText": return withCards(ctx, node.Data || "");
  case "NodeSoftBreak": return " ";
  case "NodeHeading": return el(`h${Math.max(1, Math.min(6, node.HeadingLevel || 1))}`, {}, children());
  case "NodeList": return node.ListData?.Typ === 1
    ? el("ol", {start: node.ListData.Start}, children()) : el("ul", {}, children());
  case "NodeCodeSpan": return el("code", {}, content(node, "NodeCodeSpanContent"));
  case "NodeCodeBlock": return el("pre", {}, el("code", {}, content(node, "NodeCodeBlockCode")));
  case "NodeLink": {
    const href = safeLink(content(node, "NodeLinkDest"));
    return href ? el("a", {href, target:"_blank", rel:"noopener noreferrer"}, children()) : children();
  }
  case "NodeImage": return children();  // Alt text only. No remote or local resource is loaded.
  case "NodeTable": return el("div", {class:"scroll-x"}, el("table", {}, children()));
  case "NodeTableHead": return el("thead", {}, (node.Children || []).map(row =>
    el("tr", {}, (row.Children || []).map(cell => el("th", {scope:"col"}, renderNode(ctx, cell))))));
  case "NodeTableRow": return el("tr", {}, (node.Children || []).map(cell => el("td", {}, renderNode(ctx, cell))));
  default: return children();  // Document, table cells and formatting containers; discard syntax markers.
  }
}

export async function answerBody(ctx, source, details = ""){
  const className = "doc ansbody";
  try {
    const lute = await loadLute(ctx);
    if (lute){
      const parser = lute.New();
      parser.SetEmoji(false);
      parser.SetInlineMath(false);
      parser.SetGFMTaskListItem(false);
      parser.SetYamlFrontMatter(false);
      const blocks = text => renderNode(ctx, JSON.parse(parser.RenderJSON(text)));
      return ctx.el("div", {class:className}, blocks(source), details ? blocks(details) : null);
    }
  } catch { /* Keep the text visible even if the bundled parser is unavailable. */ }
  return ctx.el("div", {class:className + " ansbody-plain"}, source + (details ? "\n\n" + details : ""));
}
