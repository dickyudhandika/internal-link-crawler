#!/usr/bin/env python3
import argparse
import csv
import sys
import time
import re
from collections import deque
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed


# ------------------------
# URL normalization helpers
# ------------------------

def normalize_path(path: str) -> str:
    """
    Normalize a path for matching:
    - Ensure it starts with '/'
    - Remove trailing slash except for root
    """
    if not path:
        return "/"
    if not path.startswith("/"):
        # Treat bare paths as paths (no scheme/host)
        path = "/" + path
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return path


def get_normalized_path_from_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path or "/"
    return normalize_path(path)


def is_internal_url(url: str, base_netloc: str, base_scheme: str) -> bool:
    """
    Internal URLs are:
    - Same scheme + host as base
    - Or relative paths
    """
    parsed = urlparse(url)
    if not parsed.netloc:
        # Relative URL
        return True
    return parsed.scheme == base_scheme and parsed.netloc == base_netloc


def is_ignored_href(href: str) -> bool:
    if not href:
        return True
    href = href.strip()
    if href.startswith("#"):
        return True
    if href.startswith("mailto:"):
        return True
    if href.startswith("tel:"):
        return True
    if href.startswith("javascript:"):
        return True
    return False


# ------------------------
# Redirects loading
# ------------------------

def load_redirects_map(redirects_file: str):
    """
    Load redirects CSV with at least columns: from, to
    Build map: redirect_from[path] -> to_url (absolute or relative)
    Works on normalized paths derived from 'from'.
    Extra columns are ignored.
    """
    redirect_from = {}

    with open(redirects_file, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if "from" not in reader.fieldnames or "to" not in reader.fieldnames:
            raise ValueError("Redirects CSV must have 'from' and 'to' columns.")
        for row in reader:
            from_raw = (row.get("from") or "").strip()
            to_raw = (row.get("to") or "").strip()
            if not from_raw or not to_raw:
                continue
            # Use only path part of 'from' for key, normalize
            parsed_from = urlparse(from_raw)
            from_path = parsed_from.path or "/"
            key = normalize_path(from_path)
            redirect_from[key] = to_raw

    return redirect_from


# ------------------------
# Input URLs loading
# ------------------------

def load_urls_from_sitemap(sitemap_url_or_path: str):
    """
    Load URLs from sitemap.xml (remote URL or local file).
    Very simple <loc> extractor, enough for standard sitemaps.
    """
    if sitemap_url_or_path.startswith("http://") or sitemap_url_or_path.startswith("https://"):
        print(f"[INFO] Fetching sitemap from URL: {sitemap_url_or_path}")
        resp = requests.get(sitemap_url_or_path, timeout=20)
        resp.raise_for_status()
        content = resp.text
    else:
        print(f"[INFO] Reading sitemap from file: {sitemap_url_or_path}")
        with open(sitemap_url_or_path, encoding="utf-8") as f:
            content = f.read()

    # Rough extraction of <loc>...</loc> content
    loc_pattern = re.compile(r"<loc>(.*?)</loc>", re.IGNORECASE)
    urls = [m.group(1).strip() for m in loc_pattern.finditer(content)]
    print(f"[INFO] Loaded {len(urls)} URLs from sitemap")
    return urls


def load_urls_from_file(urls_file: str):
    print(f"[INFO] Reading URLs from file: {urls_file}")
    urls = []
    with open(urls_file, encoding="utf-8") as f:
        for line in f:
            url = line.strip()
            if not url:
                continue
            urls.append(url)
    print(f"[INFO] Loaded {len(urls)} URLs from urls file")
    return urls


# ------------------------
# HTTP fetching
# ------------------------

def make_session():
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=10,
        pool_maxsize=10,
        max_retries=3,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({
        "User-Agent": "InternalLinkRedirectCrawler/1.0"
    })
    return session


def fetch_url(session, url: str, timeout: int = 20):
    """
    Fetch a URL with basic retry handled by the session adapter.
    Follows redirects automatically.
    Returns (final_url, text) or (None, None) on failure.
    """
    try:
        resp = session.get(url, timeout=timeout, allow_redirects=True)
        final_url = resp.url
        if resp.status_code >= 400:
            print(f"[WARN] HTTP {resp.status_code} for {url} -> {final_url}")
            return final_url, None
        return final_url, resp.text
    except requests.RequestException as e:
        print(f"[ERROR] Failed to fetch {url}: {e}")
        return None, None


# ------------------------
# Link extraction
# ------------------------

def collapse_whitespace(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def extract_links_from_html(html: str, page_url: str, base_netloc: str, base_scheme: str):
    """
    Extract internal <a> links from HTML.
    Returns list of (target_url, anchor_text).
    """
    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.find_all("a")

    links = []
    for a in anchors:
        href = a.get("href")
        if is_ignored_href(href):
            continue
        absolute = urljoin(page_url, href)
        if not is_internal_url(absolute, base_netloc, base_scheme):
            print(f"[DEBUG] Skipping external URL: {absolute}")
            continue
        anchor_text = collapse_whitespace(a.get_text())
        links.append((absolute, anchor_text))

    return links


# ------------------------
# Crawler core
# ------------------------

def crawl(base_url: str,
          start_urls,
          redirects_map,
          max_pages: int = 5000,
          concurrency: int = 5,
          delay_between_batches: float = 0.0):

    parsed_base = urlparse(base_url)
    base_netloc = parsed_base.netloc
    base_scheme = parsed_base.scheme

    session = make_session()

    visited = set()
    to_crawl = deque()

    for u in start_urls:
        to_crawl.append(u)

    internal_links_rows = []
    redirect_issues_rows = []

    pages_crawled = 0

    print(f"[INFO] Starting crawl with max_pages={max_pages}, concurrency={concurrency}")

    while to_crawl and pages_crawled < max_pages:
        batch = []
        while to_crawl and len(batch) < concurrency and pages_crawled + len(batch) < max_pages:
            url = to_crawl.popleft()
            # Normalize by final URL later; for now, dedupe requested URL set
            if url in visited:
                continue
            batch.append(url)

        if not batch:
            break

        print(f"[INFO] Fetching batch of {len(batch)} URLs")
        futures = {}
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            for url in batch:
                futures[executor.submit(fetch_url, session, url)] = url

            for future in as_completed(futures):
                original_url = futures[future]
                final_url, html = future.result()
                if not final_url:
                    visited.add(original_url)
                    continue

                normalized_final = final_url
                if normalized_final in visited:
                    visited.add(original_url)
                    continue

                visited.add(original_url)
                visited.add(normalized_final)

                if html is None:
                    continue

                pages_crawled += 1
                print(f"[INFO] Crawled {normalized_final} (page {pages_crawled})")

                # Extract links
                links = extract_links_from_html(html, normalized_final, base_netloc, base_scheme)

                for target_url, anchor_text in links:
                    # For crawling queue: only internal and not visited
                    if target_url not in visited:
                        to_crawl.append(target_url)

                    target_path_norm = get_normalized_path_from_url(target_url)
                    redirect_target = ""
                    if target_path_norm in redirects_map:
                        redirect_target = redirects_map[target_path_norm]

                    # Row for internal_links.csv
                    internal_links_rows.append({
                        "source_url": normalized_final,
                        "target_url": target_url,
                        "anchor_text": anchor_text,
                        "redirect_target": redirect_target
                    })

                    # Row for redirect_issues.csv (only if link points to old 301 URL)
                    if redirect_target:
                        redirect_issues_rows.append({
                            "source_url": normalized_final,
                            "old_target_url": target_url,
                            "correct_target_url": redirect_target,
                            "anchor_text": anchor_text
                        })

        if delay_between_batches > 0:
            time.sleep(delay_between_batches)

    return pages_crawled, internal_links_rows, redirect_issues_rows


# ------------------------
# CSV writing
# ------------------------

def write_internal_links_csv(rows, filename="internal_links.csv"):
    fieldnames = ["source_url", "target_url", "anchor_text", "redirect_target"]
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    print(f"[INFO] Wrote {len(rows)} rows to {filename}")


def write_redirect_issues_csv(rows, filename="redirect_issues.csv"):
    fieldnames = ["source_url", "old_target_url", "correct_target_url", "anchor_text"]
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    print(f"[INFO] Wrote {len(rows)} rows to {filename}")


# ------------------------
# CLI
# ------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Redirect-aware internal link crawler for a single domain."
    )
    parser.add_argument("--base-url", required=True,
                        help="Base site URL, e.g. https://www.example.com")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--sitemap", help="Sitemap XML URL or local file path")
    group.add_argument("--urls-file", help="Text file with one URL per line")
    parser.add_argument("--redirects-file", required=True,
                        help="CSV file with at least 'from' and 'to' columns")
    parser.add_argument("--max-pages", type=int, default=5000,
                        help="Maximum number of pages to crawl (default: 5000)")
    parser.add_argument("--concurrency", type=int, default=5,
                        help="Number of concurrent HTTP requests (default: 5)")
    parser.add_argument("--delay", type=float, default=0.0,
                        help="Optional delay in seconds between batches (default: 0.0)")
    parser.add_argument("--internal-links-output", default="internal_links.csv",
                        help="Output CSV path for all internal links (default: internal_links.csv)")
    parser.add_argument("--redirect-issues-output", default="redirect_issues.csv",
                        help="Output CSV path for redirect issues (default: redirect_issues.csv)")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    base_url = args.base_url.rstrip("/")
    print(f"[INFO] Base URL: {base_url}")

    # Load redirects
    print(f"[INFO] Loading redirects from: {args.redirects_file}")
    redirects_map = load_redirects_map(args.redirects_file)
    print(f"[INFO] Loaded {len(redirects_map)} redirect rules")

    # Load starting URLs
    if args.sitemap:
        start_urls = load_urls_from_sitemap(args.sitemap)
    else:
        start_urls = load_urls_from_file(args.urls_file)

    # Ensure all start URLs are absolute and internal
    parsed_base = urlparse(base_url)
    base_netloc = parsed_base.netloc
    base_scheme = parsed_base.scheme

    abs_start_urls = []
    for u in start_urls:
        abs_url = urljoin(base_url, u)
        if not is_internal_url(abs_url, base_netloc, base_scheme):
            print(f"[WARN] Skipping non-internal start URL: {abs_url}")
            continue
        abs_start_urls.append(abs_url)

    print(f"[INFO] Starting URLs count (internal only): {len(abs_start_urls)}")

    pages_crawled, internal_links_rows, redirect_issues_rows = crawl(
        base_url=base_url,
        start_urls=abs_start_urls,
        redirects_map=redirects_map,
        max_pages=args.max_pages,
        concurrency=args.concurrency,
        delay_between_batches=args.delay,
    )

    # Write outputs
    write_internal_links_csv(internal_links_rows, filename=args.internal_links_output)
    write_redirect_issues_csv(redirect_issues_rows, filename=args.redirect_issues_output)

    # Summary
    print("\n[SUMMARY]")
    print(f"Pages crawled: {pages_crawled}")
    print(f"Total internal links found: {len(internal_links_rows)}")
    print(f"Links that need updating (redirect issues): {len(redirect_issues_rows)}")


if __name__ == "__main__":
    main()
