#!/usr/bin/env python3
"""
Flatten a GitHub repo into a single static HTML page for fast skimming and Ctrl+F.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import webbrowser
from typing import List

from render.html_generator import build_html
from render.import_graph import build_import_graph
from render.ontology import FileInfo, RenderDecision, is_likely_generated_path
from render.util import bytes_human

MAX_DEFAULT_BYTES = 50 * 1024
CHARS_PER_TOKEN = 4
MIN_KEEP_LINES = 20
DEFAULT_MAX_TOKENS = 0  # 0 = unlimited
BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico",
    ".pdf", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".mp3", ".mp4", ".mov", ".avi", ".mkv", ".wav", ".ogg", ".flac",
    ".ttf", ".otf", ".eot", ".woff", ".woff2",
    ".so", ".dll", ".dylib", ".class", ".jar", ".exe", ".bin",
}


def run(cmd: List[str], cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, capture_output=True)


def git_clone(url: str, dst: str) -> None:
    run(["git", "clone", "--depth", "1", url, dst])


def git_head_commit(repo_dir: str) -> str:
    try:
        cp = run(["git", "rev-parse", "HEAD"], cwd=repo_dir)
        return cp.stdout.strip()
    except Exception:
        return "(unknown)"


def looks_binary(path: pathlib.Path) -> bool:
    ext = path.suffix.lower()
    if ext in BINARY_EXTENSIONS:
        return True
    try:
        with path.open("rb") as f:
            chunk = f.read(8192)
        if b"\x00" in chunk:
            return True
        try:
            chunk.decode("utf-8")
        except UnicodeDecodeError:
            return True
        return False
    except Exception:
        return True


def decide_file(
    path: pathlib.Path,
    repo_root: pathlib.Path,
    max_bytes: int,
    *,
    include_generated: bool,
) -> FileInfo:
    rel = str(path.relative_to(repo_root)).replace(os.sep, "/")
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        size = 0
    if "/.git/" in f"/{rel}/" or rel.startswith(".git/"):
        return FileInfo(path, rel, size, RenderDecision(False, "ignored"))
    if not include_generated and is_likely_generated_path(rel):
        return FileInfo(path, rel, size, RenderDecision(False, "ignored"))
    if max_bytes > 0 and size > max_bytes:
        return FileInfo(path, rel, size, RenderDecision(False, "too_large"))
    if looks_binary(path):
        return FileInfo(path, rel, size, RenderDecision(False, "binary"))
    return FileInfo(path, rel, size, RenderDecision(True, "ok"))


def git_tracked_files(repo_root: pathlib.Path) -> set[str] | None:
    try:
        cp = run(["git", "ls-files"], cwd=str(repo_root))
        return {line for line in cp.stdout.splitlines() if line}
    except Exception:
        return None


def collect_files(
    repo_root: pathlib.Path,
    max_bytes: int,
    git_only: bool = False,
    *,
    include_generated: bool = False,
) -> List[FileInfo]:
    tracked: set[str] | None = None
    if git_only:
        tracked = git_tracked_files(repo_root)

    infos: List[FileInfo] = []
    for p in sorted(repo_root.rglob("*")):
        if p.is_symlink():
            continue
        if p.is_file():
            if tracked is not None:
                rel = str(p.relative_to(repo_root)).replace(os.sep, "/")
                if rel not in tracked:
                    continue
            infos.append(decide_file(p, repo_root, max_bytes, include_generated=include_generated))
    return infos


def read_text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def truncate_lines(text: str, keep_lines: int) -> tuple[str, int]:
    lines = text.splitlines(keepends=True)
    total = len(lines)
    if total <= keep_lines:
        return text, 0
    head_n = keep_lines // 2
    tail_n = keep_lines - head_n
    omitted = total - keep_lines
    head = lines[:head_n]
    tail = lines[-tail_n:] if tail_n > 0 else []
    marker = f"\n... ({omitted} lines omitted to fit token budget) ...\n\n"
    return "".join(head) + marker + "".join(tail), omitted


def load_file_contents(infos: List[FileInfo]) -> dict[str, str]:
    contents: dict[str, str] = {}
    for i in infos:
        if i.decision.include:
            try:
                contents[i.rel] = read_text(i.path)
            except Exception as e:
                contents[i.rel] = f"Failed to read: {e}"
    return contents


def fit_to_token_budget(
    contents: dict[str, str],
    max_tokens: int,
) -> tuple[dict[str, str], int, int]:
    content_budget = int(max_tokens * 0.90)
    total_content_tokens = sum(estimate_tokens(t) for t in contents.values())
    if total_content_tokens <= content_budget:
        return contents, 0, 0

    ratio = content_budget / total_content_tokens
    result: dict[str, str] = {}
    total_omitted = 0
    files_truncated = 0

    for rel, text in contents.items():
        line_count = len(text.splitlines())
        target_lines = max(MIN_KEEP_LINES, int(line_count * ratio))
        truncated, omitted = truncate_lines(text, target_lines)
        result[rel] = truncated
        if omitted > 0:
            total_omitted += omitted
            files_truncated += 1

    return result, total_omitted, files_truncated


def derive_temp_output_path(repo_url: str) -> pathlib.Path:
    parts = repo_url.rstrip("/").split("/")
    if len(parts) >= 2:
        repo_name = parts[-1]
        if repo_name.endswith(".git"):
            repo_name = repo_name[:-4]
        filename = f"{repo_name}.html"
    else:
        filename = "repo.html"

    return pathlib.Path(tempfile.gettempdir()) / filename


def is_local_path(source: str) -> bool:
    p = pathlib.Path(source)
    return p.exists()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Flatten a GitHub repo to a single HTML page",
        epilog=(
            "By default, common generated paths (build/, dist/, __pycache__/, .mypy_cache/, "
            "*.egg-info/) are skipped so the outline stays readable. "
            "Root-level *.html files are not auto-skipped. Use --include-generated to render them."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("source", help="GitHub repo URL or local directory path")
    ap.add_argument("-o", "--out", help="Output HTML file path (default: temporary file derived from repo name)")
    ap.add_argument("--max-bytes", type=int, default=MAX_DEFAULT_BYTES, help="Max file size to render (bytes); larger files are listed but skipped")
    ap.add_argument(
        "-t",
        "--max-tokens",
        type=int,
        default=DEFAULT_MAX_TOKENS,
        help="Max total tokens (approx) for output; 0 = unlimited. Longest files get middle-truncated to fit budget while keeping the full directory tree.",
    )
    ap.add_argument(
        "--include-generated",
        action="store_true",
        help="Include paths normally skipped as generated (build/, dist/, egg-info, __pycache__, .mypy_cache/)",
    )
    ap.add_argument("--no-open", action="store_true", help="Don't open the HTML file in browser after generation")
    args = ap.parse_args()

    local = is_local_path(args.source)

    if local:
        source_path = pathlib.Path(args.source).resolve()
        if source_path.is_file():
            print(f"Error: '{args.source}' is a file, not a directory. Pass a repo directory or a GitHub URL.", file=sys.stderr)
            return 1
        if not source_path.is_dir():
            print(f"Error: '{args.source}' is not a directory. Pass a repo directory or a GitHub URL.", file=sys.stderr)
            return 1
        repo_label = source_path.name
    else:
        source_path = pathlib.Path()
        repo_label = args.source

    if args.out is None:
        if local:
            args.out = str(pathlib.Path(tempfile.gettempdir()) / f"{source_path.name}.html")
        else:
            args.out = str(derive_temp_output_path(args.source))

    tmpdir: str | None = None

    try:
        if local:
            repo_dir = source_path
            head = git_head_commit(str(repo_dir))
            print(f"📂 Using local directory: {repo_dir}", file=sys.stderr)
            if head != "(unknown)":
                print(f"✓ Git HEAD: {head[:8]}", file=sys.stderr)
        else:
            tmpdir = tempfile.mkdtemp(prefix="flatten_repo_")
            repo_dir = pathlib.Path(tmpdir, "repo")
            print(f"📁 Cloning {args.source} to temporary directory: {repo_dir}", file=sys.stderr)
            git_clone(args.source, str(repo_dir))
            head = git_head_commit(str(repo_dir))
            print(f"✓ Clone complete (HEAD: {head[:8]})", file=sys.stderr)

        print(f"📊 Scanning files in {repo_dir}...", file=sys.stderr)
        infos = collect_files(
            repo_dir,
            args.max_bytes,
            git_only=local,
            include_generated=args.include_generated,
        )
        rendered_count = sum(1 for i in infos if i.decision.include)
        skipped_count = len(infos) - rendered_count
        print(f"✓ Found {len(infos)} files total ({rendered_count} will be rendered, {skipped_count} skipped)", file=sys.stderr)

        contents = load_file_contents(infos)
        total_tokens = sum(estimate_tokens(t) for t in contents.values())
        print(f"📏 Estimated {total_tokens:,} tokens across {len(contents)} files", file=sys.stderr)

        if args.max_tokens > 0 and total_tokens > int(args.max_tokens * 0.90):
            contents, omitted_lines, truncated_files = fit_to_token_budget(contents, args.max_tokens)
            new_total = sum(estimate_tokens(t) for t in contents.values())
            print(f"✂️  Truncated {truncated_files} files ({omitted_lines:,} lines omitted) to fit {args.max_tokens:,} token budget → ~{new_total:,} tokens", file=sys.stderr)

        print(f"🔗 Building import graph...", file=sys.stderr)
        import_graph = build_import_graph(infos, contents)
        print(f"✓ Graph: {len(import_graph.nodes)} modules, {len(import_graph.edges)} imports", file=sys.stderr)

        print(f"🔨 Generating HTML...", file=sys.stderr)
        html_out = build_html(repo_label, repo_dir, head, infos, contents, import_graph)

        out_path = pathlib.Path(args.out)
        print(f"💾 Writing HTML file: {out_path.resolve()}", file=sys.stderr)
        out_path.write_text(html_out, encoding="utf-8")
        file_size = out_path.stat().st_size
        print(f"✓ Wrote {bytes_human(file_size)} to {out_path}", file=sys.stderr)

        if not args.no_open:
            print(f"🌐 Opening {out_path} in browser...", file=sys.stderr)
            webbrowser.open(f"file://{out_path.resolve()}")

        if tmpdir:
            print(f"🗑️  Cleaning up temporary directory: {tmpdir}", file=sys.stderr)
        return 0
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
