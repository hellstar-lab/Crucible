"""Scan one PDF with Shield and print a readable report (manual testing).

    python scripts/scan.py path/to/file.pdf
"""

from dotenv import load_dotenv

load_dotenv()  # entry point only: library code reads os.environ

import sys  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.shield import ShieldError, scan_pdf  # noqa: E402

MAX_FRAGMENT_CHARS = 200
PREVIEW_CHARS = 300


def show(text: str) -> str:
    """Truncate, and escape invisible characters (zero-width, bidi) so they show."""
    text = text if len(text) <= MAX_FRAGMENT_CHARS else text[:MAX_FRAGMENT_CHARS] + "..."
    return text if text.isprintable() else ascii(text)


def main(argv: list[str]) -> int:
    if len(argv) != 1 or not Path(argv[0]).is_file():
        msg = "usage: python scripts/scan.py path/to/file.pdf" if len(argv) != 1 else f"file not found: {argv[0]}"
        print(f"error: {msg}", file=sys.stderr)
        return 2 if len(argv) != 1 else 1
    start = time.perf_counter()
    try:
        result = scan_pdf(argv[0])
    except ShieldError as exc:  # encrypted, corrupt, over page limit, ...
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"file:      {argv[0]}")
    print(f"status:    {result.status}")
    print(f"fragments: {len(result.hidden_fragments)}")
    for i, f in enumerate(result.hidden_fragments, 1):
        where = "metadata" if f.page == 0 else f"page {f.page}"
        print(f"  [{i}] {where} | {', '.join(f.reasons)}\n      {show(f.text)}")
    print(f"\nvisible_text (first {PREVIEW_CHARS} chars):\n{result.visible_text[:PREVIEW_CHARS]}")
    print(f"\nscan time: {time.perf_counter() - start:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
