# md2pdf

Converts Markdown files (including LaTeX math) to PDF.

## Usage

1. Put your `.md` files in the `inputs/` folder.
2. Run:

   ```bash
   .venv/bin/python md2pdf.py
   ```

3. Your PDFs appear in the `outputs/` folder.

Files that haven't changed since their last conversion are skipped. Use `--force` to rebuild everything.
