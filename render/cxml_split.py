"""Split a saved ontogit CXML export into token-bounded chunks for LLM paste.

Each chunk is a well-formed <documents>...</documents> wrapper. Document
boundaries match ``generate_cxml_text`` in ``html_generator.py``. If a source
file contains the literal substring ``</document>``, regex-based splitting
can mis-cut; that is the same limitation as the export format itself.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Keep in sync with CHARS_PER_TOKEN in rendergit.py
CHARS_PER_TOKEN = 4

_DOCUMENT_RE = re.compile(
    r'(<document index="[^"]+">.*?</document>)',
    re.DOTALL,
)


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def split_cxml_documents(content: str) -> list[str]:
    return _DOCUMENT_RE.findall(content)


def split_cxml_to_chunks(
    documents: list[str],
    tokens_per_chunk: int,
) -> list[list[str]]:
    chunks: list[list[str]] = []
    current_chunk: list[str] = []
    current_tokens = 0

    for i, doc in enumerate(documents, 1):
        doc_tokens = estimate_tokens(doc)

        if doc_tokens > tokens_per_chunk:
            if current_chunk:
                chunks.append(current_chunk)
                current_chunk = []
                current_tokens = 0
            chunks.append([doc])
            continue

        if current_tokens + doc_tokens > tokens_per_chunk and current_chunk:
            chunks.append(current_chunk)
            current_chunk = []
            current_tokens = 0

        current_chunk.append(doc)
        current_tokens += doc_tokens

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def write_cxml_chunks(
    input_path: Path,
    chunks: list[list[str]],
) -> list[Path]:
    written: list[Path] = []
    n = len(chunks)
    for idx, chunk_docs in enumerate(chunks, 1):
        body = "\n".join(chunk_docs)
        output = f"<documents>\n{body}\n</documents>\n"
        out_path = input_path.with_name(
            f"{input_path.stem}_part{idx}_of{n}{input_path.suffix}"
        )
        out_path.write_text(output, encoding="utf-8")
        written.append(out_path)
    return written


def split_cxml_file(
    input_path: Path,
    *,
    tokens_per_chunk: int,
) -> list[Path]:
    content = input_path.read_text(encoding="utf-8", errors="replace")
    documents = split_cxml_documents(content)
    if not documents:
        raise ValueError(
            f"No <document>...</document> blocks found in {input_path}. "
            "Expected ontogit LLM-view CXML."
        )
    chunks = split_cxml_to_chunks(documents, tokens_per_chunk)
    return write_cxml_chunks(input_path, chunks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Split a large CXML file (ontogit LLM view) into ~N-token chunks "
            "on whole <document> boundaries."
        )
    )
    parser.add_argument(
        "input",
        type=Path,
        help="Path to the saved .txt / .cxml file",
    )
    parser.add_argument(
        "-t",
        "--tokens",
        type=int,
        default=100_000,
        help="Approximate max tokens per chunk (char count / 4), default 100000",
    )
    args = parser.parse_args(argv)

    path = args.input
    if not path.is_file():
        print(f"File not found: {path}", file=sys.stderr)
        return 1

    print(f"Loading {path.name}...", file=sys.stderr)
    content = path.read_text(encoding="utf-8", errors="replace")
    documents = split_cxml_documents(content)
    print(f"Found {len(documents)} documents", file=sys.stderr)

    for i, doc in enumerate(documents, 1):
        dt = estimate_tokens(doc)
        if dt > args.tokens:
            print(
                f"Warning: document {i} is ~{dt:,} tokens (>{args.tokens:,}) "
                f"— written as its own chunk",
                file=sys.stderr,
            )

    chunks = split_cxml_to_chunks(documents, args.tokens)
    paths = write_cxml_chunks(path, chunks)

    for idx, out_path in enumerate(paths, 1):
        token_count = sum(
            estimate_tokens(d) for d in chunks[idx - 1]
        )
        print(
            f"Part {idx}/{len(paths)} -> {out_path.name} (~{token_count:,} tokens)",
            file=sys.stderr,
        )

    print(f"Done: {len(paths)} file(s).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
