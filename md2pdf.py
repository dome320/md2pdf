#!/usr/bin/env python3
"""Convert every Markdown file in ./inputs to a PDF in ./outputs.

Markdown is turned into HTML (with LaTeX math kept intact), then printed to
PDF by headless Chromium, which typesets the math with MathJax.

Usage:
    python md2pdf.py            # convert all .md files in inputs/
    python md2pdf.py --force    # re-convert even if the PDF is up to date
    python md2pdf.py --html     # also save the intermediate HTML (for debugging)
"""

import argparse
import html
import re
import sys
import tempfile
import warnings
from pathlib import Path
from urllib.parse import unquote, urlparse

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

import markdown
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR / "inputs"
OUTPUT_DIR = BASE_DIR / "outputs"
MATHJAX_JS = BASE_DIR / "vendor" / "mathjax" / "tex-svg-full.js"
MATHJAX_URL = "https://cdn.jsdelivr.net/npm/mathjax@3.2.2/es5/tex-svg-full.js"

MD_EXTENSIONS = [
    "tables",
    "footnotes",
    "attr_list",
    "def_list",
    "abbr",
    "md_in_html",
    "toc",
    "mdx_truly_sane_lists",   # allows 2-space nested list indentation
    "pymdownx.superfences",   # fenced code blocks (also inside lists)
    "pymdownx.highlight",     # syntax highlighting via Pygments
    "pymdownx.arithmatex",    # protects $...$ / $$...$$ math from Markdown
]

MD_EXTENSION_CONFIGS = {
    # smart_dollar: "$17.0B ... $16B" is treated as money, not math.
    "pymdownx.arithmatex": {"generic": True, "smart_dollar": True},
    "pymdownx.highlight": {"use_pygments": True, "css_class": "highlight"},
    "footnotes": {"BACKLINK_TEXT": "&#8617;&#xFE0E;"},
}

CSS = """
@page { size: Letter; margin: 2cm 2cm 2.2cm 2cm; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body {
    font-family: -apple-system, "Helvetica Neue", Helvetica, Arial, sans-serif;
    font-size: 10.5pt; line-height: 1.5; color: #1f2328; margin: 0;
}
h1, h2, h3, h4, h5, h6 { line-height: 1.25; margin: 1.4em 0 0.5em; break-after: avoid; }
h1 { font-size: 21pt; border-bottom: 1px solid #d0d7de; padding-bottom: 0.25em; margin-top: 0; }
h2 { font-size: 15.5pt; border-bottom: 1px solid #eaeef2; padding-bottom: 0.2em; }
h3 { font-size: 12.5pt; }
h4, h5, h6 { font-size: 11pt; }
p, ul, ol, dl, blockquote, pre, table { margin: 0 0 0.8em; }
li + li { margin-top: 0.2em; }
li > p { margin-bottom: 0.4em; }
a { color: #0969da; text-decoration: none; }
hr { border: 0; border-top: 1px solid #d0d7de; margin: 1.4em 0; }
img { max-width: 100%; }
.missing-image { color: #9a6700; font-style: italic; border: 1px dashed #d4a72c;
                 padding: 0.4em 0.6em; display: inline-block; }
blockquote { color: #57606a; border-left: 0.25em solid #d0d7de; margin-left: 0; padding: 0 1em; }

code { font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 0.88em;
       background: #f2f4f6; border-radius: 4px; padding: 0.1em 0.35em; }
pre { background: #f6f8fa; border: 1px solid #e1e4e8; border-radius: 6px;
      padding: 0.8em 1em; white-space: pre-wrap; word-wrap: break-word; break-inside: avoid; }
pre code { background: none; padding: 0; font-size: 0.85em; }
.highlight pre { margin: 0; }
.highlight { margin-bottom: 0.8em; }

table { border-collapse: collapse; max-width: 100%; font-size: 9pt; line-height: 1.35; }
thead { display: table-header-group; }
tr { break-inside: avoid; }
th, td { border: 1px solid #d0d7de; padding: 0.35em 0.6em; vertical-align: top; }
th { background: #f6f8fa; font-weight: 600; }
tbody tr:nth-child(even) td { background: #fbfcfd; }

/* Math */
div.arithmatex { margin: 0.6em 0 1em; text-align: center; break-inside: avoid; }
mjx-container[display="true"] { margin: 0 !important; }
mjx-container[display="true"] > svg { max-width: 100%; height: auto; }

.footnote { font-size: 9pt; color: #57606a; }
"""

MATHJAX_CONFIG = r"""
window.MathJax = {
  tex: {
    inlineMath: [['\\(', '\\)']],
    displayMath: [['\\[', '\\]']],
    processEscapes: false
  },
  options: { ignoreHtmlClass: 'mathjax-ignore', processHtmlClass: 'arithmatex' },
  svg: { fontCache: 'global' }
};
"""

# After math is typeset: shrink inline formulas and tables wider than the page
# (otherwise Chromium scales the *whole* document down to fit them).
FIT_TABLES_JS = """
() => {
  const width = document.body.clientWidth;
  for (const c of document.querySelectorAll('mjx-container:not([display="true"])')) {
    const block = c.closest('p, li, td, th, div') || document.body;
    const svg = c.querySelector('svg');
    if (svg && svg.getBoundingClientRect().width > block.clientWidth) {
      c.style.display = 'block';
      c.style.margin = '0.3em 0';
      svg.style.maxWidth = '100%';
      svg.style.height = 'auto';
    }
  }
  for (const t of document.querySelectorAll('table')) {
    let size = parseFloat(getComputedStyle(t).fontSize);
    while (t.scrollWidth > width && size > 5.5) {
      size -= 0.25;
      t.style.fontSize = size + 'px';
    }
  }
}
"""

FOOTER_TEMPLATE = """
<div style="width:100%; font-size:8px; color:#8c959f; text-align:center;
            font-family:-apple-system,Helvetica,Arial,sans-serif;">
  Page <span class="pageNumber"></span> of <span class="totalPages"></span>
</div>
"""

LIST_ITEM_RE = re.compile(r"^ {0,3}(?:[-*+]|\d+[.)])\s+\S")
FENCE_RE = re.compile(r"^\s*(```|~~~)")


def add_blank_lines_before_lists(md_text: str) -> str:
    """Python-Markdown needs a blank line before a list; GitHub doesn't.

    Insert one when a list starts right after a paragraph line, so lists
    written GitHub-style aren't merged into the preceding paragraph.
    """
    out, in_fence, prev = [], False, ""
    for line in md_text.splitlines():
        if FENCE_RE.match(line):
            in_fence = not in_fence
        if (not in_fence and LIST_ITEM_RE.match(line) and prev.strip()
                and not LIST_ITEM_RE.match(prev) and not prev.startswith((" ", "\t", "|"))):
            out.append("")
        out.append(line)
        prev = line
    return "\n".join(out) + "\n"


CODE_SPAN_RE = re.compile(r"(`+)(?:.+?)\1")
DISPLAY_INLINE_RE = re.compile(r"\$\$.+?\$\$")


def escape_currency_dollars(md_text: str) -> str:
    """Turn every `$` that isn't a math delimiter into the HTML entity &#36;.

    Uses Pandoc's rules for inline math: an opening `$` must be followed by a
    non-space, and a closing `$` must be preceded by a non-space and not be
    followed by a digit. So "$17.0B ... $16B" stays money, while "$x_t$" is math.
    `$$ ... $$` display blocks and code are left untouched.
    """
    out, in_fence, in_display = [], False, False
    for line in md_text.splitlines():
        stripped = line.strip()
        if FENCE_RE.match(line) and not in_display:
            in_fence = not in_fence
        if in_fence:
            out.append(line)
            continue
        if in_display:
            out.append(line)
            if "$$" in stripped:
                in_display = False
            continue
        if stripped.startswith("$$") and stripped.count("$$") == 1:
            in_display = True  # multi-line $$ block starts here
            out.append(line)
            continue

        # Hide code spans and one-line $$...$$ from the scan.
        protected = []

        def hide(m):
            protected.append(m.group(0))
            return f"\x00{len(protected) - 1}\x00"

        work = DISPLAY_INLINE_RE.sub(hide, CODE_SPAN_RE.sub(hide, line))

        dollars = [i for i, c in enumerate(work) if c == "$" and (i == 0 or work[i - 1] != "\\")]
        literal, k = set(), 0
        while k < len(dollars):
            i = dollars[k]
            opener_ok = i + 1 < len(work) and not work[i + 1].isspace()
            close_idx = None
            if opener_ok:
                for m in range(k + 1, len(dollars)):
                    j = dollars[m]
                    after = work[j + 1] if j + 1 < len(work) else ""
                    if not work[j - 1].isspace() and not after.isdigit():
                        close_idx = m
                        break
            if close_idx is None:
                literal.add(i)
                k += 1
            else:
                k = close_idx + 1
        work = "".join("&#36;" if i in literal else c for i, c in enumerate(work))
        out.append(re.sub(r"\x00(\d+)\x00", lambda m: protected[int(m.group(1))], work))
    return "\n".join(out) + "\n"


def mark_missing_images(body: str, md_dir: Path):
    """Replace <img> tags whose local file doesn't exist with a visible note."""
    missing = []

    def repl(m):
        tag = m.group(0)
        src = re.search(r'src="([^"]*)"', tag)
        if not src:
            return tag
        uri = html.unescape(src.group(1))
        if urlparse(uri).scheme in ("http", "https", "data"):
            return tag
        path = Path(unquote(urlparse(uri).path))
        if not path.is_absolute():
            path = md_dir / path
        if path.exists():
            return tag
        missing.append(uri)
        alt = re.search(r'alt="([^"]*)"', tag)
        label = alt.group(1) if alt and alt.group(1) else "image"
        return f'<span class="missing-image">[Image not found: {label} — {html.escape(uri)}]</span>'

    body = re.sub(r"<img\b[^>]*>", repl, body)
    return body, missing


def pygments_css() -> str:
    from pygments.formatters import HtmlFormatter
    return HtmlFormatter(style="default").get_style_defs(".highlight")


def build_html(md_path: Path):
    md_text = md_path.read_text(encoding="utf-8")
    md_text = escape_currency_dollars(add_blank_lines_before_lists(md_text))
    body = markdown.markdown(
        md_text,
        extensions=MD_EXTENSIONS,
        extension_configs=MD_EXTENSION_CONFIGS,
        output_format="html",
    )
    body, missing = mark_missing_images(body, md_path.parent)
    doc = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>{html.escape(md_path.stem)}</title>
<base href="{md_path.parent.as_uri()}/">
<style>{CSS}{pygments_css()}</style>
<script>{MATHJAX_CONFIG}</script>
<script src="{MATHJAX_JS.as_uri()}"></script>
</head>
<body class="mathjax-ignore">
{body}
</body></html>"""
    return doc, missing


def convert(page, md_path: Path, pdf_path: Path, keep_html: bool) -> list:
    doc, missing = build_html(md_path)
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as tmp:
        tmp.write(doc)
        tmp_path = Path(tmp.name)
    try:
        page.goto(tmp_path.as_uri(), wait_until="load")
        page.wait_for_function("window.MathJax && MathJax.startup && MathJax.startup.promise")
        page.evaluate("MathJax.startup.promise")
        errors = page.evaluate(
            "Array.from(document.querySelectorAll('mjx-merror'))"
            ".map(e => e.getAttribute('data-mjx-error') || e.textContent)"
        )
        page.evaluate(FIT_TABLES_JS)
        page.pdf(
            path=str(pdf_path),
            format="Letter",
            print_background=True,
            prefer_css_page_size=True,
            display_header_footer=True,
            header_template="<span></span>",
            footer_template=FOOTER_TEMPLATE,
        )
        if keep_html:
            pdf_path.with_suffix(".html").write_text(doc, encoding="utf-8")
    finally:
        tmp_path.unlink(missing_ok=True)

    return [f"image not found: {m}" for m in missing] + [f"math error: {e}" for e in errors]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="re-convert files even if the PDF is newer")
    parser.add_argument("--html", action="store_true", help="also save the intermediate HTML next to the PDF")
    args = parser.parse_args()

    if not MATHJAX_JS.exists():
        print(f"MathJax not found. Download it with:\n"
              f"  mkdir -p {MATHJAX_JS.parent} && curl -L -o {MATHJAX_JS} {MATHJAX_URL}", file=sys.stderr)
        return 1

    INPUT_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)

    md_files = sorted(p for p in INPUT_DIR.rglob("*") if p.suffix.lower() in {".md", ".markdown"})
    if not md_files:
        print(f"No Markdown files found in {INPUT_DIR}")
        return 0

    todo, skipped = [], 0
    for md_path in md_files:
        rel = md_path.relative_to(INPUT_DIR)
        pdf_path = (OUTPUT_DIR / rel).with_suffix(".pdf")
        if not args.force and pdf_path.exists() and pdf_path.stat().st_mtime >= md_path.stat().st_mtime:
            print(f"  skip   {rel} (up to date)")
            skipped += 1
        else:
            todo.append((rel, md_path, pdf_path))

    converted = failed = 0
    if todo:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--allow-file-access-from-files"])
            # Lay out at the printable width (Letter minus 2cm margins) so the
            # table/formula fitting step measures what will actually print.
            page = browser.new_page(viewport={"width": round((8.5 - 4 / 2.54) * 96), "height": 1000})
            page.emulate_media(media="print")
            for rel, md_path, pdf_path in todo:
                pdf_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    problems = convert(page, md_path, pdf_path, args.html)
                except Exception as exc:  # keep going with the remaining files
                    print(f"  FAILED {rel}: {exc}", file=sys.stderr)
                    failed += 1
                    continue
                print(f"  ok     {rel} -> {pdf_path.relative_to(BASE_DIR)}")
                for msg in problems:
                    print(f"           warning: {msg}")
                converted += 1
            browser.close()

    print(f"\nDone: {converted} converted, {skipped} skipped, {failed} failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
