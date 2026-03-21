#!/usr/bin/env python3
"""
Tavily-powered provider documentation crawler.

Uses Tavily's map() to discover pages, then extract() to get content,
saving each page as a markdown file in a structured directory.

Usage:
    python tavily-docs-crawler.py <docs_url> <provider_name> [--api-key KEY] [--limit N] [--max-depth N]

Examples:
    python tavily-docs-crawler.py https://developerhub.ppro.com ppro
    python tavily-docs-crawler.py https://developer.paysafe.com paysafe --limit 100
    python tavily-docs-crawler.py https://docs.nuvei.com nuvei --max-depth 3
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from tavily import TavilyClient

# Default output base directory
DEFAULT_OUTPUT_BASE = os.path.expanduser("~/.pay-knowledge/.secrets/provider-docs")

# Default API key location
DEFAULT_KEY_FILE = os.path.expanduser("~/.pay-knowledge/.secrets/tavily-keys.json")


def load_api_key(explicit_key: str | None = None) -> str:
    """Load Tavily API key from argument, env var, or key file."""
    if explicit_key:
        return explicit_key

    env_key = os.environ.get("TAVILY_API_KEY")
    if env_key:
        return env_key

    if os.path.exists(DEFAULT_KEY_FILE):
        with open(DEFAULT_KEY_FILE) as f:
            keys = json.load(f)
            if isinstance(keys, list) and keys:
                return keys[0]
            elif isinstance(keys, dict) and "key" in keys:
                return keys["key"]

    print("ERROR: No Tavily API key found. Provide via --api-key, TAVILY_API_KEY env var, or key file.")
    sys.exit(1)


def url_to_filename(url: str) -> str:
    """Convert a URL to a safe filename."""
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    if not path:
        path = "index"
    # Replace path separators and special chars
    filename = re.sub(r"[^\w\-.]", "_", path)
    # Collapse multiple underscores
    filename = re.sub(r"_+", "_", filename)
    # Trim length
    if len(filename) > 150:
        filename = filename[:150]
    return filename + ".md"


def discover_pages(client: TavilyClient, base_url: str, limit: int, max_depth: int) -> list[str]:
    """Use Tavily map() to discover all pages on the docs site."""
    print(f"\n[MAP] Discovering pages at {base_url} (limit={limit}, depth={max_depth})...")

    try:
        response = client.map(
            url=base_url,
            max_depth=max_depth,
            max_breadth=100,
            limit=limit,
        )
    except Exception as e:
        print(f"  ERROR during map(): {e}")
        return []

    urls = response.get("results", [])
    usage = response.get("usage", {})
    resp_time = response.get("response_time", 0)

    print(f"  Found {len(urls)} pages in {resp_time:.1f}s")
    if usage:
        print(f"  Credits used for mapping: {usage}")

    return urls


def extract_pages(
    client: TavilyClient,
    urls: list[str],
    output_dir: Path,
    batch_size: int = 20,
) -> dict:
    """Use Tavily extract() to get content from discovered pages."""
    stats = {
        "total": len(urls),
        "success": 0,
        "failed": 0,
        "credits": 0,
        "errors": [],
    }

    output_dir.mkdir(parents=True, exist_ok=True)

    # Process in batches of up to 20 (API limit)
    for i in range(0, len(urls), batch_size):
        batch = urls[i : i + batch_size]
        batch_num = i // batch_size + 1
        total_batches = (len(urls) + batch_size - 1) // batch_size

        print(f"\n[EXTRACT] Batch {batch_num}/{total_batches} ({len(batch)} URLs)...")

        try:
            response = client.extract(
                urls=batch,
                extract_depth="basic",
                format="markdown",
                include_images=False,
            )
        except Exception as e:
            print(f"  ERROR during extract(): {e}")
            stats["failed"] += len(batch)
            stats["errors"].append(str(e))
            continue

        # Process successful results
        results = response.get("results", [])
        for result in results:
            url = result.get("url", "unknown")
            content = result.get("raw_content", "")

            if not content:
                stats["failed"] += 1
                continue

            filename = url_to_filename(url)
            filepath = output_dir / filename

            # Write markdown with frontmatter
            with open(filepath, "w") as f:
                f.write(f"---\nurl: {url}\ncrawled_at: {time.strftime('%Y-%m-%dT%H:%M:%S')}\n---\n\n")
                f.write(content)

            stats["success"] += 1
            print(f"  Saved: {filename} ({len(content)} chars)")

        # Process failures
        failed = response.get("failed_results", [])
        for fail in failed:
            url = fail.get("url", "unknown")
            error = fail.get("error", "unknown error")
            stats["failed"] += 1
            stats["errors"].append(f"{url}: {error}")
            print(f"  FAILED: {url} - {error}")

        # Track usage
        usage = response.get("usage", {})
        if usage:
            stats["credits"] += usage.get("extract_credits", 0)

        # Rate limit courtesy - small pause between batches
        if i + batch_size < len(urls):
            time.sleep(1)

    return stats


def main():
    parser = argparse.ArgumentParser(description="Crawl provider documentation using Tavily API")
    parser.add_argument("url", help="Base documentation URL to crawl")
    parser.add_argument("provider", help="Provider name (used for output directory)")
    parser.add_argument("--api-key", help="Tavily API key")
    parser.add_argument("--limit", type=int, default=50, help="Max pages to discover (default: 50)")
    parser.add_argument("--max-depth", type=int, default=2, help="Max crawl depth (1-5, default: 2)")
    parser.add_argument("--output-base", default=DEFAULT_OUTPUT_BASE, help="Base output directory")
    parser.add_argument("--batch-size", type=int, default=20, help="Extract batch size (max 20)")
    parser.add_argument("--dry-run", action="store_true", help="Only discover pages, don't extract")

    args = parser.parse_args()

    # Load API key
    api_key = load_api_key(args.api_key)
    client = TavilyClient(api_key=api_key)

    # Output directory
    output_dir = Path(args.output_base) / args.provider
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Provider: {args.provider}")
    print(f"URL: {args.url}")
    print(f"Output: {output_dir}")

    # Step 1: Discover pages
    urls = discover_pages(client, args.url, args.limit, args.max_depth)

    if not urls:
        print("\nNo pages discovered. Check the URL and try again.")
        sys.exit(1)

    # Save discovered URLs for reference
    urls_file = output_dir / "_discovered_urls.json"
    with open(urls_file, "w") as f:
        json.dump(urls, f, indent=2)
    print(f"\nSaved URL list to {urls_file}")

    if args.dry_run:
        print("\n[DRY RUN] Skipping extraction. Discovered URLs:")
        for url in urls:
            print(f"  {url}")
        return

    # Step 2: Extract content
    stats = extract_pages(client, urls, output_dir, args.batch_size)

    # Step 3: Summary
    print("\n" + "=" * 60)
    print("CRAWL SUMMARY")
    print("=" * 60)
    print(f"Provider:    {args.provider}")
    print(f"Base URL:    {args.url}")
    print(f"Discovered:  {len(urls)} pages")
    print(f"Extracted:   {stats['success']}/{stats['total']} pages")
    print(f"Failed:      {stats['failed']}")
    print(f"Output dir:  {output_dir}")

    # Credit cost estimate
    map_credits = max(1, len(urls) // 10)
    extract_credits = max(1, stats["success"] // 5)
    total_credits = map_credits + extract_credits
    print("\nEstimated credits:")
    print(f"  Map:     ~{map_credits} credits ({len(urls)} pages / 10)")
    print(f"  Extract: ~{extract_credits} credits ({stats['success']} pages / 5)")
    print(f"  Total:   ~{total_credits} credits")

    if stats["errors"]:
        print(f"\nErrors ({len(stats['errors'])}):")
        for err in stats["errors"][:10]:
            print(f"  - {err}")
        if len(stats["errors"]) > 10:
            print(f"  ... and {len(stats['errors']) - 10} more")

    # Save summary
    summary_file = output_dir / "_crawl_summary.json"
    with open(summary_file, "w") as f:
        json.dump(
            {
                "provider": args.provider,
                "base_url": args.url,
                "discovered": len(urls),
                "extracted": stats["success"],
                "failed": stats["failed"],
                "estimated_credits": total_credits,
                "errors": stats["errors"][:20],
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            },
            f,
            indent=2,
        )

    print(f"\nSummary saved to {summary_file}")


if __name__ == "__main__":
    main()
