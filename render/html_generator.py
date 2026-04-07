from __future__ import annotations

import html
import json
import pathlib
from typing import List

import markdown
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_for_filename, TextLexer

from render.ontology import FileInfo, flat_render_order, ordered_groups
from render.import_graph import ImportGraph
from render.util import bytes_human, slugify

MARKDOWN_EXTENSIONS = {".md", ".markdown", ".mdown", ".mkd", ".mkdn"}


def read_text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def render_markdown_text(md_text: str) -> str:
    return markdown.markdown(md_text, extensions=["fenced_code", "tables", "toc"])  # type: ignore


def highlight_code(text: str, filename: str, formatter: HtmlFormatter) -> str:
    try:
        lexer = get_lexer_for_filename(filename, stripall=False)
    except Exception:
        lexer = TextLexer(stripall=False)
    return highlight(text, lexer, formatter)


def try_tree_command(repo_dir: pathlib.Path) -> str:
    import subprocess

    try:
        cp = subprocess.run(
            ["tree", "-a", "."],
            cwd=str(repo_dir),
            check=True,
            text=True,
            capture_output=True,
        )
        return cp.stdout
    except Exception:
        return _generate_tree_fallback(repo_dir)


def _generate_tree_fallback(root: pathlib.Path) -> str:
    lines: list[str] = []

    def walk(dir_path: pathlib.Path, prefix: str = "") -> None:
        entries = [e for e in dir_path.iterdir() if e.name != ".git"]
        entries.sort(key=lambda e: (not e.is_dir(), e.name.lower()))
        for i, e in enumerate(entries):
            last = i == len(entries) - 1
            branch = "└── " if last else "├── "
            lines.append(prefix + branch + e.name)
            if e.is_dir():
                extension = "    " if last else "│   "
                walk(e, prefix + extension)

    lines.append(root.name)
    walk(root)
    return "\n".join(lines)


def build_grouped_toc_html(groups: list[tuple[str, str, list[FileInfo]]]) -> str:
    """One nested TOC snippet for sidebar and toc-top (identical HTML)."""
    parts: list[str] = []
    for group_id, label, files in groups:
        gslug = slugify(group_id)
        inner = []
        for i in files:
            anchor = slugify(i.rel)
            inner.append(
                f'<li><a href="#file-{anchor}">{html.escape(i.rel)}</a> '
                f'<span class="muted">({bytes_human(i.size)})</span></li>'
            )
        inner_html = "\n".join(inner)
        parts.append(
            f'<li class="toc-group">'
            f'<details open class="toc-details">'
            f'<summary class="toc-summary">'
            f'<a href="#group-{html.escape(gslug, quote=True)}">{html.escape(label)}</a> '
            f'<span class="muted">({len(files)})</span>'
            f"</summary>"
            f'<ul class="toc toc-nested">\n{inner_html}\n</ul>'
            f"</details>"
            f"</li>"
        )
    return "\n".join(parts)


def generate_cxml_text(
    infos: List[FileInfo],
    repo_dir: pathlib.Path,
    contents: dict[str, str] | None = None,
) -> str:
    lines = ["<documents>"]
    for index, i in enumerate(flat_render_order(infos), 1):
        lines.append(f'<document index="{index}">')
        lines.append(f"<source>{i.rel}</source>")
        lines.append("<document_content>")
        if contents and i.rel in contents:
            lines.append(contents[i.rel])
        else:
            try:
                lines.append(read_text(i.path))
            except Exception as e:
                lines.append(f"Failed to read: {str(e)}")
        lines.append("</document_content>")
        lines.append("</document>")
    lines.append("</documents>")
    return "\n".join(lines)


def build_html(
    repo_url: str,
    repo_dir: pathlib.Path,
    head_commit: str,
    infos: List[FileInfo],
    contents: dict[str, str] | None = None,
    import_graph: ImportGraph | None = None,
) -> str:
    formatter = HtmlFormatter(nowrap=False)
    pygments_css = formatter.get_style_defs(".highlight")

    rendered = [i for i in infos if i.decision.include]
    groups = ordered_groups(infos)
    skipped_binary = [i for i in infos if i.decision.reason == "binary"]
    skipped_large = [i for i in infos if i.decision.reason == "too_large"]
    skipped_ignored = [i for i in infos if i.decision.reason == "ignored"]
    total_files = len(rendered) + len(skipped_binary) + len(skipped_large) + len(skipped_ignored)

    tree_text = try_tree_command(repo_dir)
    cxml_text = generate_cxml_text(infos, repo_dir, contents)
    toc_html = build_grouped_toc_html(groups)

    graph_json = import_graph.to_json() if import_graph else '{"nodes":[],"links":[]}'
    has_graph = import_graph is not None and len(import_graph.nodes) > 0

    sections: list[str] = []
    omit_marker = "lines omitted to fit token budget"
    for group_id, label, files in groups:
        gslug = slugify(group_id)
        file_sections: list[str] = []
        for i in files:
            anchor = slugify(i.rel)
            p = i.path
            ext = p.suffix.lower()
            try:
                text = contents[i.rel] if contents and i.rel in contents else read_text(p)
                was_truncated = omit_marker in text
                if ext in MARKDOWN_EXTENSIONS:
                    body_html = render_markdown_text(text)
                else:
                    code_html = highlight_code(text, i.rel, formatter)
                    body_html = f'<div class="highlight">{code_html}</div>'
                if was_truncated:
                    body_html = (
                        f'<div class="truncation-banner">File truncated to fit token budget</div>{body_html}'
                    )
            except Exception as e:
                body_html = f'<pre class="error">Failed to render: {html.escape(str(e))}</pre>'
            file_sections.append(f"""
<section class="file-section" id="file-{anchor}">
  <h3>{html.escape(i.rel)} <span class="muted">({bytes_human(i.size)})</span></h3>
  <div class="file-body">{body_html}</div>
  <div class="back-top"><a href="#top">back to top</a></div>
</section>
""")
        sections.append(
            f"""
<section class="repo-group" id="group-{html.escape(gslug, quote=True)}">
  <h2 class="repo-group-title">{html.escape(label)} <span class="muted">({len(files)} files)</span></h2>
  <div class="repo-group-body">
    {''.join(file_sections)}
  </div>
</section>
"""
        )

    def render_skip_list(title: str, items: List[FileInfo]) -> str:
        if not items:
            return ""
        lis = [
            f"<li><code>{html.escape(i.rel)}</code> "
            f"<span class='muted'>({bytes_human(i.size)})</span></li>"
            for i in items
        ]
        return (
            f"<details open><summary>{html.escape(title)} ({len(items)})</summary>"
            f"<ul class='skip-list'>\n" + "\n".join(lis) + "\n</ul></details>"
        )

    skipped_html = render_skip_list("Skipped binaries", skipped_binary) + render_skip_list(
        "Skipped large files", skipped_large
    )

    # Collect unique groups for the legend
    group_set: list[str] = []
    seen_groups: set[str] = set()
    if import_graph:
        for n in import_graph.nodes:
            if n.group not in seen_groups:
                seen_groups.add(n.group)
                group_set.append(n.group)

    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{html.escape(repo_url)} — ontogit</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; }}

  :root {{
    --bg-primary: #0d1117;
    --bg-secondary: #161b22;
    --bg-tertiary: #1c2128;
    --bg-card: #21262d;
    --border-primary: #30363d;
    --border-secondary: #21262d;
    --text-primary: #e6edf3;
    --text-secondary: #8b949e;
    --text-muted: #6e7681;
    --accent-blue: #58a6ff;
    --accent-purple: #bc8cff;
    --accent-green: #3fb950;
    --accent-orange: #d29922;
    --accent-red: #f85149;
    --accent-cyan: #39d2c0;
    --accent-pink: #f778ba;
    --link: #58a6ff;
    --graph-bg: #0d1117;
    --sidebar-bg: #161b22;
    --radius: 8px;
  }}

  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
    margin: 0; padding: 0; line-height: 1.6;
    color: var(--text-primary);
    background: var(--bg-primary);
  }}

  a {{ color: var(--link); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}

  .muted {{ color: var(--text-muted); font-weight: normal; font-size: 0.9em; }}

  /* ── Layout ─────────────────────────────────────────────── */
  .page {{ display: grid; grid-template-columns: minmax(260px, 300px) minmax(0,1fr); gap: 0; min-height: 100vh; }}

  #sidebar {{
    position: sticky; top: 0; align-self: start;
    height: 100vh; overflow-y: auto;
    border-right: 1px solid var(--border-primary);
    background: var(--sidebar-bg);
    scrollbar-width: thin;
    scrollbar-color: var(--border-primary) transparent;
  }}
  #sidebar .sidebar-inner {{ padding: 1rem 0.85rem; }}
  #sidebar h2 {{
    margin: 0 0 0.75rem 0; font-size: 0.8rem;
    text-transform: uppercase; letter-spacing: 0.08em;
    color: var(--text-muted);
  }}

  .sidebar-brand {{
    padding: 1rem 0.85rem 0.5rem;
    border-bottom: 1px solid var(--border-primary);
    margin-bottom: 0.5rem;
  }}
  .sidebar-brand h1 {{
    margin: 0; font-size: 1.1rem; font-weight: 700;
    background: linear-gradient(135deg, var(--accent-cyan), var(--accent-blue), var(--accent-purple));
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    background-clip: text;
  }}
  .sidebar-brand .repo-name {{
    font-size: 0.85rem; color: var(--text-secondary);
    margin-top: 0.15rem;
  }}

  .toc {{ list-style: none; padding-left: 0; margin: 0; }}
  .toc > li {{ padding: 0.08rem 0; }}
  .toc a {{ color: var(--text-secondary); font-size: 0.85rem; }}
  .toc a:hover {{ color: var(--accent-blue); }}
  .toc-nested {{
    list-style: none; padding-left: 0.65rem; margin: 0.25rem 0 0.4rem 0;
    border-left: 2px solid var(--border-primary);
  }}
  .toc-nested li {{
    padding: 0.1rem 0; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis; max-width: 100%;
  }}
  .toc-details {{ margin-bottom: 0.3rem; }}
  .toc-summary {{
    cursor: pointer; font-weight: 600; font-size: 0.85rem;
    color: var(--text-primary);
  }}
  .toc-summary a {{ font-weight: 600; color: var(--text-primary); }}

  main {{ padding: 0; }}

  /* ── Header bar ─────────────────────────────────────────── */
  .header-bar {{
    padding: 1rem 1.5rem;
    border-bottom: 1px solid var(--border-primary);
    background: var(--bg-secondary);
    display: flex; align-items: center; justify-content: space-between;
    flex-wrap: wrap; gap: 0.75rem;
  }}
  .header-meta {{ font-size: 0.85rem; color: var(--text-secondary); }}
  .header-meta strong {{ color: var(--text-primary); }}

  /* ── View toggle ────────────────────────────────────────── */
  .view-toggle {{
    display: flex; gap: 0; border-radius: var(--radius); overflow: hidden;
    border: 1px solid var(--border-primary);
  }}
  .toggle-btn {{
    padding: 0.45rem 1rem;
    border: none;
    background: var(--bg-tertiary);
    color: var(--text-secondary);
    cursor: pointer; font-size: 0.85rem; font-weight: 500;
    transition: all 0.15s ease;
  }}
  .toggle-btn:not(:last-child) {{ border-right: 1px solid var(--border-primary); }}
  .toggle-btn.active {{
    background: var(--accent-blue);
    color: #fff;
  }}
  .toggle-btn:hover:not(.active) {{ background: var(--bg-card); color: var(--text-primary); }}

  /* ── Graph view ─────────────────────────────────────────── */
  #graph-view {{
    position: relative;
    background: var(--graph-bg);
  }}
  #graph-container {{
    width: 100%;
    height: calc(100vh - 60px);
    cursor: grab;
  }}
  #graph-container:active {{ cursor: grabbing; }}
  #graph-container svg {{ display: block; }}

  .graph-controls {{
    position: absolute; top: 1rem; right: 1rem;
    display: flex; flex-direction: column; gap: 0.4rem; z-index: 10;
  }}
  .graph-btn {{
    width: 36px; height: 36px;
    border: 1px solid var(--border-primary);
    background: var(--bg-card);
    color: var(--text-primary);
    border-radius: var(--radius);
    cursor: pointer; font-size: 1.1rem;
    display: flex; align-items: center; justify-content: center;
    transition: background 0.15s;
  }}
  .graph-btn:hover {{ background: var(--bg-tertiary); }}

  .graph-legend {{
    position: absolute; bottom: 1rem; left: 1rem;
    background: var(--bg-card);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius);
    padding: 0.75rem 1rem;
    z-index: 10;
    max-width: 280px;
  }}
  .graph-legend h4 {{
    margin: 0 0 0.5rem 0; font-size: 0.75rem;
    text-transform: uppercase; letter-spacing: 0.06em;
    color: var(--text-muted);
  }}
  .legend-item {{
    display: flex; align-items: center; gap: 0.5rem;
    font-size: 0.8rem; color: var(--text-secondary);
    padding: 0.15rem 0;
  }}
  .legend-dot {{
    width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0;
  }}

  .graph-stats {{
    position: absolute; top: 1rem; left: 1rem;
    background: var(--bg-card);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius);
    padding: 0.6rem 0.85rem;
    z-index: 10;
  }}
  .graph-stats .stat {{
    font-size: 0.8rem; color: var(--text-secondary);
    display: flex; gap: 0.5rem; align-items: center;
  }}
  .graph-stats .stat-val {{
    font-weight: 700; color: var(--text-primary); font-size: 1rem;
  }}

  .node-tooltip {{
    position: absolute;
    background: var(--bg-card);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius);
    padding: 0.65rem 0.85rem;
    pointer-events: none;
    z-index: 20;
    font-size: 0.8rem;
    color: var(--text-secondary);
    box-shadow: 0 4px 24px rgba(0,0,0,0.4);
    max-width: 320px;
    display: none;
  }}
  .node-tooltip .tt-name {{
    font-weight: 700; color: var(--text-primary); font-size: 0.9rem;
    margin-bottom: 0.3rem; word-break: break-all;
  }}
  .node-tooltip .tt-row {{ display: flex; justify-content: space-between; gap: 1rem; }}
  .node-tooltip .tt-label {{ color: var(--text-muted); }}

  /* ── Human view (code) ──────────────────────────────────── */
  #human-view {{ display: none; }}
  #human-view.active {{ display: block; }}

  .content-pad {{ padding: 1rem 1.5rem 2rem; }}

  pre {{
    background: var(--bg-tertiary); padding: 0.75rem;
    overflow: auto; border-radius: var(--radius);
    border: 1px solid var(--border-primary);
    color: var(--text-primary);
  }}
  code {{
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 0.85em;
  }}
  .highlight {{ overflow-x: auto; border-radius: var(--radius); }}

  .repo-group {{
    padding: 0 1.5rem 1.25rem;
    border-top: 1px solid var(--border-primary);
  }}
  .repo-group-title {{
    margin: 1rem 0 0.75rem; font-size: 1.1rem; font-weight: 600;
    color: var(--text-primary);
    border-bottom: 2px solid var(--accent-blue);
    padding-bottom: 0.35rem; display: inline-block;
  }}
  .repo-group-body {{
    background: var(--bg-secondary); border-radius: var(--radius);
    border: 1px solid var(--border-primary); overflow: hidden;
  }}
  .file-section {{ padding: 1rem; border-top: 1px solid var(--border-secondary); }}
  .file-section:first-child {{ border-top: none; }}
  .file-section h3 {{ margin: 0 0 0.5rem 0; font-size: 0.95rem; font-weight: 600; }}
  .file-body {{ margin-bottom: 0.5rem; }}
  .back-top {{ font-size: 0.8rem; }}
  .skip-list code {{ background: var(--bg-tertiary); padding: 0.1rem 0.3rem; border-radius: 4px; }}
  .error {{ color: var(--accent-red); background: #2d1517; }}
  .truncation-banner {{
    background: #2d2305; border: 1px solid #5c4a0a; border-radius: 6px;
    padding: 0.4rem 0.75rem; margin-bottom: 0.5rem; font-size: 0.85rem;
    color: var(--accent-orange);
  }}

  .toc-top {{ display: block; }}
  @media (min-width: 1000px) {{ .toc-top {{ display: none; }} }}
  :target {{ scroll-margin-top: 8px; }}

  /* ── LLM view ───────────────────────────────────────────── */
  #llm-view {{ display: none; padding: 1.5rem; }}
  #llm-text {{
    width: 100%; height: 70vh;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 0.85em;
    border: 1px solid var(--border-primary);
    border-radius: var(--radius);
    padding: 1rem; resize: vertical;
    background: var(--bg-secondary);
    color: var(--text-primary);
  }}
  .copy-hint {{ margin-top: 0.5rem; color: var(--text-muted); font-size: 0.9em; }}

  /* ── Pygments dark override ─────────────────────────────── */
  .highlight pre {{ background: var(--bg-tertiary); border: 1px solid var(--border-primary); }}
  .highlight .hll {{ background-color: #2d333b; }}
  .highlight .c, .highlight .cm, .highlight .c1, .highlight .cs {{ color: #8b949e; font-style: italic; }}
  .highlight .k, .highlight .kn, .highlight .kp, .highlight .kr, .highlight .kd {{ color: #ff7b72; }}
  .highlight .kt {{ color: #ff7b72; }}
  .highlight .s, .highlight .s1, .highlight .s2, .highlight .sb, .highlight .sc, .highlight .sd {{ color: #a5d6ff; }}
  .highlight .si {{ color: #a5d6ff; }}
  .highlight .se {{ color: #79c0ff; }}
  .highlight .nb {{ color: #ffa657; }}
  .highlight .nf, .highlight .fm {{ color: #d2a8ff; }}
  .highlight .nc {{ color: #f0c674; }}
  .highlight .nn {{ color: #ffa657; }}
  .highlight .nd {{ color: #d2a8ff; }}
  .highlight .na {{ color: #79c0ff; }}
  .highlight .no {{ color: #79c0ff; }}
  .highlight .ni {{ color: #e6edf3; }}
  .highlight .ne {{ color: #ffa657; }}
  .highlight .o {{ color: #ff7b72; }}
  .highlight .p {{ color: #e6edf3; }}
  .highlight .mi, .highlight .mf, .highlight .mh, .highlight .mo {{ color: #79c0ff; }}
  .highlight .bp {{ color: #79c0ff; }}
  .highlight .ow {{ color: #ff7b72; }}

  {pygments_css}
</style>
</head>
<body>
<a id="top"></a>

<div class="page">
  <nav id="sidebar">
    <div class="sidebar-brand">
      <h1>ontogit</h1>
      <div class="repo-name">{html.escape(repo_url)}</div>
    </div>
    <div class="sidebar-inner">
      <h2>Files ({len(rendered)})</h2>
      <ul class="toc toc-sidebar">
        {toc_html}
      </ul>
    </div>
  </nav>

  <div>
    <div class="header-bar">
      <div class="header-meta">
        <strong>{total_files}</strong> files &middot;
        <strong>{len(rendered)}</strong> rendered &middot;
        commit <code>{html.escape(head_commit[:8])}</code>
      </div>
      <div class="view-toggle">
        <button type="button" class="toggle-btn{'  active' if has_graph else ''}" onclick="showView('graph', event)" {'style="display:none"' if not has_graph else ''}>Graph</button>
        <button type="button" class="toggle-btn{'' if has_graph else ' active'}" onclick="showView('human', event)">Code</button>
        <button type="button" class="toggle-btn" onclick="showView('llm', event)">LLM</button>
      </div>
    </div>

    <main>

    <!-- ── Graph View ───────────────────────────────────── -->
    <div id="graph-view" {'style="display:block"' if has_graph else 'style="display:none"'}>
      <div id="graph-container"></div>
      <div class="graph-controls">
        <button class="graph-btn" onclick="zoomIn()" title="Zoom in">+</button>
        <button class="graph-btn" onclick="zoomOut()" title="Zoom out">&minus;</button>
        <button class="graph-btn" onclick="resetZoom()" title="Fit to view">&#x2922;</button>
      </div>
      <div class="graph-stats">
        <div class="stat"><span class="tt-label">Modules</span> <span class="stat-val" id="stat-nodes">0</span></div>
        <div class="stat"><span class="tt-label">Imports</span> <span class="stat-val" id="stat-edges">0</span></div>
      </div>
      <div class="graph-legend" id="graph-legend"></div>
      <div class="node-tooltip" id="tooltip"></div>
    </div>

    <!-- ── Human View ───────────────────────────────────── -->
    <div id="human-view" {'' if has_graph else 'class="active"'}>
      <div class="content-pad">
        <section>
          <h2>Directory tree</h2>
          <pre>{html.escape(tree_text)}</pre>
        </section>

        <section class="toc-top">
          <h2>Table of contents ({len(rendered)})</h2>
          <ul class="toc">{toc_html}</ul>
        </section>

        <section>
          <h2>Skipped items</h2>
          {skipped_html}
        </section>
      </div>

      {''.join(sections)}
    </div>

    <!-- ── LLM View ─────────────────────────────────────── -->
    <div id="llm-view">
      <section>
        <h2>LLM View — CXML Format</h2>
        <p style="color:var(--text-secondary)">Copy the text below and paste it to an LLM for analysis:</p>
        <textarea id="llm-text" readonly>{html.escape(cxml_text)}</textarea>
        <div class="copy-hint">
          Tip: Click in the text area and press Ctrl+A then Ctrl+C to copy all.
        </div>
      </section>
    </div>

    </main>
  </div>
</div>

<script>
// ── View switching ──────────────────────────────────────────
function showView(view, ev) {{
  document.getElementById('graph-view').style.display = 'none';
  document.getElementById('human-view').className = '';
  document.getElementById('llm-view').style.display = 'none';

  if (view === 'graph') document.getElementById('graph-view').style.display = 'block';
  else if (view === 'human') document.getElementById('human-view').className = 'active';
  else if (view === 'llm') document.getElementById('llm-view').style.display = 'block';

  document.querySelectorAll('.toggle-btn').forEach(b => b.classList.remove('active'));
  if (ev && ev.target) ev.target.classList.add('active');

  if (view === 'llm') {{
    setTimeout(() => {{ const t = document.getElementById('llm-text'); t.focus(); t.select(); }}, 100);
  }}
}}
</script>

<!-- D3.js v7 -->
<script src="https://d3js.org/d3.v7.min.js"></script>

<script>
(function() {{
  const graphData = {graph_json};
  if (!graphData.nodes.length) return;

  const container = document.getElementById('graph-container');
  const tooltip = document.getElementById('tooltip');
  const width = container.clientWidth;
  const height = container.clientHeight || (window.innerHeight - 60);

  document.getElementById('stat-nodes').textContent = graphData.nodes.length;
  document.getElementById('stat-edges').textContent = graphData.links.length;

  // ── Color palette by group ──────────────────────────────
  const palette = [
    '#58a6ff', '#bc8cff', '#3fb950', '#d29922', '#f85149',
    '#39d2c0', '#f778ba', '#79c0ff', '#ffa657', '#a5d6ff',
    '#d2a8ff', '#7ee787', '#e6b422', '#ff9bce', '#56d4dd',
  ];
  const groups = [...new Set(graphData.nodes.map(n => n.group))].sort();
  const groupColor = {{}};
  groups.forEach((g, i) => {{ groupColor[g] = palette[i % palette.length]; }});

  // ── Build legend ────────────────────────────────────────
  const legendEl = document.getElementById('graph-legend');
  let legendHTML = '<h4>Modules</h4>';
  groups.forEach(g => {{
    legendHTML += '<div class="legend-item">' +
      '<span class="legend-dot" style="background:' + groupColor[g] + '"></span>' +
      '<span>' + g + '</span></div>';
  }});
  legendEl.innerHTML = legendHTML;

  // ── Degree for sizing ───────────────────────────────────
  const degree = {{}};
  graphData.nodes.forEach(n => degree[n.id] = 0);
  graphData.links.forEach(l => {{
    const s = typeof l.source === 'object' ? l.source.id : l.source;
    const t = typeof l.target === 'object' ? l.target.id : l.target;
    degree[s] = (degree[s] || 0) + 1;
    degree[t] = (degree[t] || 0) + 1;
  }});
  const maxDeg = Math.max(1, ...Object.values(degree));

  function nodeRadius(d) {{
    const deg = degree[d.id] || 0;
    return 4 + 14 * Math.sqrt(deg / maxDeg);
  }}

  // ── SVG + zoom ──────────────────────────────────────────
  const svg = d3.select(container).append('svg')
    .attr('width', width)
    .attr('height', height);

  const defs = svg.append('defs');
  defs.append('marker')
    .attr('id', 'arrowhead')
    .attr('viewBox', '0 -4 8 8')
    .attr('refX', 20)
    .attr('refY', 0)
    .attr('markerWidth', 6)
    .attr('markerHeight', 6)
    .attr('orient', 'auto')
    .append('path')
    .attr('d', 'M0,-3L7,0L0,3')
    .attr('fill', '#30363d');

  const g = svg.append('g');

  const zoomBehavior = d3.zoom()
    .scaleExtent([0.1, 8])
    .on('zoom', (event) => g.attr('transform', event.transform));
  svg.call(zoomBehavior);

  window.zoomIn = () => svg.transition().duration(300).call(zoomBehavior.scaleBy, 1.4);
  window.zoomOut = () => svg.transition().duration(300).call(zoomBehavior.scaleBy, 0.7);
  window.resetZoom = () => {{
    const bounds = g.node().getBBox();
    const fullWidth = bounds.width || width;
    const fullHeight = bounds.height || height;
    const midX = bounds.x + fullWidth / 2;
    const midY = bounds.y + fullHeight / 2;
    const scale = 0.85 / Math.max(fullWidth / width, fullHeight / height);
    const translate = [width / 2 - midX * scale, height / 2 - midY * scale];
    svg.transition().duration(500).call(
      zoomBehavior.transform,
      d3.zoomIdentity.translate(translate[0], translate[1]).scale(scale)
    );
  }};

  // ── Simulation ──────────────────────────────────────────
  const simulation = d3.forceSimulation(graphData.nodes)
    .force('link', d3.forceLink(graphData.links).id(d => d.id).distance(80))
    .force('charge', d3.forceManyBody().strength(-200))
    .force('center', d3.forceCenter(width / 2, height / 2))
    .force('collision', d3.forceCollide().radius(d => nodeRadius(d) + 4))
    .force('x', d3.forceX(width / 2).strength(0.04))
    .force('y', d3.forceY(height / 2).strength(0.04));

  // ── Edges ───────────────────────────────────────────────
  const link = g.append('g')
    .selectAll('line')
    .data(graphData.links)
    .join('line')
    .attr('stroke', '#30363d')
    .attr('stroke-width', 1)
    .attr('stroke-opacity', 0.6)
    .attr('marker-end', 'url(#arrowhead)');

  // ── Glow filter ─────────────────────────────────────────
  const filter = defs.append('filter').attr('id', 'glow');
  filter.append('feGaussianBlur').attr('stdDeviation', '3').attr('result', 'blur');
  filter.append('feMerge').selectAll('feMergeNode')
    .data(['blur', 'SourceGraphic']).join('feMergeNode')
    .attr('in', d => d);

  // ── Nodes ───────────────────────────────────────────────
  const node = g.append('g')
    .selectAll('circle')
    .data(graphData.nodes)
    .join('circle')
    .attr('r', d => nodeRadius(d))
    .attr('fill', d => groupColor[d.group] || '#58a6ff')
    .attr('stroke', d => d3.color(groupColor[d.group] || '#58a6ff').brighter(0.6))
    .attr('stroke-width', 1.5)
    .attr('opacity', 0.9)
    .style('cursor', 'pointer')
    .call(d3.drag()
      .on('start', dragStarted)
      .on('drag', dragged)
      .on('end', dragEnded));

  // ── Labels (only for high-degree nodes) ─────────────────
  const labelThreshold = Math.max(2, maxDeg * 0.15);
  const label = g.append('g')
    .selectAll('text')
    .data(graphData.nodes.filter(d => (degree[d.id] || 0) >= labelThreshold))
    .join('text')
    .text(d => d.label)
    .attr('font-size', '10px')
    .attr('font-family', 'ui-monospace, SFMono-Regular, monospace')
    .attr('fill', '#e6edf3')
    .attr('text-anchor', 'middle')
    .attr('dy', d => -nodeRadius(d) - 4)
    .style('pointer-events', 'none')
    .style('text-shadow', '0 0 4px rgba(0,0,0,0.8), 0 0 8px rgba(0,0,0,0.5)');

  // ── Interactions ────────────────────────────────────────
  function slugify(s) {{
    return s.split('').map(c => /[a-zA-Z0-9_-]/.test(c) ? c : '-').join('');
  }}

  node.on('mouseover', function(event, d) {{
    d3.select(this).attr('filter', 'url(#glow)').attr('opacity', 1);

    // Highlight connected edges
    link.attr('stroke', l => (l.source.id === d.id || l.target.id === d.id) ? groupColor[d.group] : '#30363d')
        .attr('stroke-opacity', l => (l.source.id === d.id || l.target.id === d.id) ? 1 : 0.15)
        .attr('stroke-width', l => (l.source.id === d.id || l.target.id === d.id) ? 2 : 1);
    node.attr('opacity', n => {{
      if (n.id === d.id) return 1;
      const connected = graphData.links.some(l =>
        (l.source.id === d.id && l.target.id === n.id) ||
        (l.target.id === d.id && l.source.id === n.id));
      return connected ? 0.9 : 0.15;
    }});

    tooltip.style.display = 'block';
    tooltip.innerHTML =
      '<div class="tt-name">' + d.id + '</div>' +
      '<div class="tt-row"><span class="tt-label">Group</span><span>' + d.group + '</span></div>' +
      '<div class="tt-row"><span class="tt-label">Lines</span><span>' + d.lines + '</span></div>' +
      '<div class="tt-row"><span class="tt-label">Connections</span><span>' + (degree[d.id] || 0) + '</span></div>';
  }})
  .on('mousemove', function(event) {{
    tooltip.style.left = (event.pageX + 14) + 'px';
    tooltip.style.top = (event.pageY - 14) + 'px';
  }})
  .on('mouseout', function() {{
    d3.select(this).attr('filter', null).attr('opacity', 0.9);
    link.attr('stroke', '#30363d').attr('stroke-opacity', 0.6).attr('stroke-width', 1);
    node.attr('opacity', 0.9);
    tooltip.style.display = 'none';
  }})
  .on('click', function(event, d) {{
    const anchor = 'file-' + slugify(d.id);
    const el = document.getElementById(anchor);
    if (el) {{
      showView('human', null);
      document.querySelectorAll('.toggle-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.toggle-btn')[1].classList.add('active');
      setTimeout(() => el.scrollIntoView({{ behavior: 'smooth', block: 'start' }}), 100);
    }}
  }});

  // ── Tick ─────────────────────────────────────────────────
  simulation.on('tick', () => {{
    link
      .attr('x1', d => d.source.x)
      .attr('y1', d => d.source.y)
      .attr('x2', d => d.target.x)
      .attr('y2', d => d.target.y);
    node
      .attr('cx', d => d.x)
      .attr('cy', d => d.y);
    label
      .attr('x', d => d.x)
      .attr('y', d => d.y);
  }});

  // ── Drag ────────────────────────────────────────────────
  function dragStarted(event, d) {{
    if (!event.active) simulation.alphaTarget(0.3).restart();
    d.fx = d.x; d.fy = d.y;
  }}
  function dragged(event, d) {{ d.fx = event.x; d.fy = event.y; }}
  function dragEnded(event, d) {{
    if (!event.active) simulation.alphaTarget(0);
    d.fx = null; d.fy = null;
  }}

  // ── Initial fit ─────────────────────────────────────────
  simulation.on('end', () => {{ setTimeout(resetZoom, 200); }});
  // Also fit after a few seconds in case simulation is slow
  setTimeout(resetZoom, 3000);
}})();
</script>

</body>
</html>
"""
