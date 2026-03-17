## Internal Link Redirect Crawler

> **Note:** This README is just documentation – it does not automatically set up anything for you. You (or anyone using this script) must follow the steps in **"Setup & installation"** below on your own machine before running `crawler.py`.

This project contains a small crawler (`crawler.py`) that scans a single website for **internal links**, checks them against a **redirects CSV**, and produces two CSV reports:

- **`internal_links.csv`**: every internal link found on the site
- **`redirect_issues.csv`**: internal links that currently point at URLs that should be updated (because they match a redirect rule)

### Setup & installation

These steps assume macOS or Linux with `python3` available. On Windows, the commands are similar but activation paths differ.

1. **Clone or download this project** and open a terminal in the project root (the folder containing `crawler.py`).

2. **Create and activate a virtual environment** (recommended so you don’t pollute your global Python):

   ```bash
   cd /path/to/internal-link-crawler
   python3 -m venv .venv
   source .venv/bin/activate
   ```

   On Windows (PowerShell):

   ```powershell
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   ```

3. **Install required Python packages** inside the virtual environment:

   ```bash
   pip install requests beautifulsoup4
   ```

4. **Prepare your input files**:

   - A **redirects CSV** for `--redirects-file` with at least columns: `from,to`.
   - Either:
     - a **sitemap XML** for `--sitemap` (URL or local file), or
     - a **text file of URLs** for `--urls-file` (one URL per line).

5. **Run the crawler** from the project root (example):

   ```bash
   python3 crawler.py \
     --base-url https://www.example.com \
     --sitemap sitemap.xml \
     --redirects-file redirects.csv \
     --max-pages 2000 \
     --concurrency 5 \
     --delay 0.0 \
     --internal-links-output internal_links.csv \
     --redirect-issues-output redirect_issues.csv
   ```

After these steps, you should see `internal_links.csv` and `redirect_issues.csv` created in the project directory.

### How `crawler.py` works

**High‑level flow:**

- **1. Load redirect rules**
  - Reads a CSV file passed with `--redirects-file`.
  - The file must have at least columns: `from`, `to`.
  - The script normalizes the `from` URL to just its path (e.g. `https://example.com/old-page/` → `/old-page`) and builds a lookup map:
    - key: normalized `from` path
    - value: `to` URL (can be absolute or relative)

- **2. Load starting URLs**
  - You must choose one of:
    - `--sitemap <sitemap.xml URL or path>`: extracts all `<loc>...</loc>` URLs.
    - `--urls-file <file>`: reads a text file with one URL per line.
  - Each URL is resolved against `--base-url` and filtered so that only internal URLs (same scheme + host as `--base-url`) are used as starting points.

- **3. Crawl the site**
  - Uses a queue (BFS) and a thread pool to fetch pages concurrently.
  - For each page:
    - Follows HTTP redirects automatically (via `requests`).
    - Skips pages returning HTTP status \(\ge 400\).
    - Parses HTML using `BeautifulSoup`.
    - Extracts all `<a href="...">` links that:
      - are not `mailto:`, `tel:`, `javascript:`, `#...`, or empty
      - resolve to internal URLs (same domain as `--base-url`).
    - Each qualifying link yields:
      - `source_url`: final URL of the page where the link was found
      - `target_url`: absolute resolved URL of the link
      - `anchor_text`: collapsed human‑readable link text
      - `redirect_target`: if the link’s path matches a redirect rule’s `from` path, this is set to the corresponding `to` URL; otherwise it is empty.
    - New internal targets are added to the crawl queue until `--max-pages` is reached.

- **4. Produce outputs**
  - After crawling:
    - Writes **all internal links** to `--internal-links-output` (default `internal_links.csv`).
    - Writes **only links that match redirect rules** to `--redirect-issues-output` (default `redirect_issues.csv`).
  - Prints a short summary (pages crawled, number of internal links, number of redirect issues).

### Command‑line arguments

Run `crawler.py` from the project root:

```bash
python3 crawler.py \
  --base-url https://www.example.com \
  --sitemap sitemap.xml \
  --redirects-file redirects.csv \
  --max-pages 2000 \
  --concurrency 5 \
  --delay 0.0 \
  --internal-links-output internal_links.csv \
  --redirect-issues-output redirect_issues.csv
```

**Key arguments:**

- `--base-url` (required): Base site URL, e.g. `https://www.example.com`.
- `--sitemap` **or** `--urls-file` (one required):
  - `--sitemap`: URL or local path to `sitemap.xml`.
  - `--urls-file`: local text file with one URL per line.
- `--redirects-file` (required): Redirect rules CSV with `from`, `to` columns.
- `--max-pages`: Max pages to crawl (default `5000`).
- `--concurrency`: Number of concurrent HTTP requests (default `5`).
- `--delay`: Optional delay (seconds) between fetch batches (default `0.0`).
- `--internal-links-output`: Output path for internal links CSV (default `internal_links.csv`).
- `--redirect-issues-output`: Output path for redirect issues CSV (default `redirect_issues.csv`).

### Output formats

**1. `internal_links.csv`**

- **Columns:**
  - `source_url`: page containing the link.
  - `target_url`: full resolved URL the link points to.
  - `anchor_text`: visible text of the link (whitespace collapsed).
  - `redirect_target`: the recommended target if the link currently goes to an old redirected URL (empty string if no redirect rule matches).

**2. `redirect_issues.csv`**

- **Columns:**
  - `source_url`: page containing the outdated link.
  - `old_target_url`: current link destination on the site.
  - `correct_target_url`: the `to` URL from redirect rules (where it should point directly).
  - `anchor_text`: visible text of the link.

Every row here represents **one internal link that should be updated** in your content or templates.

### Concrete example

Assume:

- **Base URL**: `https://www.example.com`
- **Redirects CSV** (`redirects.csv`):

```csv
from,to
https://www.example.com/old-pricing,https://www.example.com/pricing
https://www.example.com/blog/old-post,https://www.example.com/blog/new-post
```

- **Sitemap** contains at least:
  - `https://www.example.com/`
  - `https://www.example.com/blog/`

- On `https://www.example.com/` the HTML includes:

```html
<a href="/old-pricing">See our pricing</a>
<a href="/about">About us</a>
```

And on `https://www.example.com/blog/` the HTML includes:

```html
<a href="https://www.example.com/blog/old-post">Read our announcement</a>
```

Run:

```bash
python3 crawler.py \
  --base-url https://www.example.com \
  --sitemap sitemap.xml \
  --redirects-file redirects.csv \
  --max-pages 50
```

**Example rows in `internal_links.csv`:**

```csv
source_url,target_url,anchor_text,redirect_target
https://www.example.com/,https://www.example.com/old-pricing,See our pricing,https://www.example.com/pricing
https://www.example.com/,https://www.example.com/about,About us,
https://www.example.com/blog/,https://www.example.com/blog/old-post,Read our announcement,https://www.example.com/blog/new-post
```

**Example rows in `redirect_issues.csv`:**

```csv
source_url,old_target_url,correct_target_url,anchor_text
https://www.example.com/,https://www.example.com/old-pricing,https://www.example.com/pricing,See our pricing
https://www.example.com/blog/,https://www.example.com/blog/old-post,https://www.example.com/blog/new-post,Read our announcement
```

From this example you can immediately see:

- Which pages (`source_url`) still link to old URLs.
- Which links should be updated to point directly at the new URLs (`correct_target_url`).

