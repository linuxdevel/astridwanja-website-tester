"""Website checker for verifying pages, links, and images on a target site.

Usage:
    python scripts/website_checker.py \
        --base-url https://example.com \
    [--internal-domains example.com,www.example.com] \
        --json-output report.json \
        --markdown-output report.md \
        --html-output report.html

The script crawls the provided base URL, collecting all internal pages. For every
page it verifies that the page loads successfully, all HTTP(S) anchors resolve
without errors (treating LinkedIn HTTP 999 responses as informational warnings),
and all images are downloadable with a valid image content type.

The script always exits with code 0. The overall status is stored in the JSON
report so that downstream steps can determine whether issues were found without
suppressing later workflow steps (e.g., sending email notifications).
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import datetime as dt
import json
import os
import sys
import textwrap
import time
from html import escape
from typing import Deque, Dict, Iterable, List, Optional, Set
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup


DEFAULT_TIMEOUT = 15
USER_AGENT = "astridwanja-website-tester/1.0 (+https://github.com/astridwanja)"


@dataclasses.dataclass
class UrlCheckResult:
    url: str
    status_code: Optional[int]
    ok: bool
    detail: Optional[str] = None
    elapsed: Optional[float] = None


@dataclasses.dataclass
class Issue:
    kind: str  # e.g., page_error, link_error, image_error, link_warning
    message: str
    source: Optional[str] = None
    target: Optional[str] = None
    status_code: Optional[int] = None


@dataclasses.dataclass
class CrawlSummary:
    base_url: str
    checked_pages: int
    checked_links: int
    checked_images: int
    duration_seconds: float
    issues: List[Issue]
    warnings: List[Issue]

    @property
    def has_issues(self) -> bool:
        return bool(self.issues)

    @property
    def has_warnings(self) -> bool:
        return bool(self.warnings)

    def to_json(self) -> Dict:
        return {
            "base_url": self.base_url,
            "checked_pages": self.checked_pages,
            "checked_links": self.checked_links,
            "checked_images": self.checked_images,
            "duration_seconds": round(self.duration_seconds, 2),
            "has_issues": self.has_issues,
            "has_warnings": self.has_warnings,
            "issues": [dataclasses.asdict(issue) for issue in self.issues],
            "warnings": [dataclasses.asdict(warning) for warning in self.warnings],
        }

    def to_markdown(self) -> str:
        header = [
            "# Website check report",
            "",
            f"**Base URL:** {self.base_url}",
            f"**Duration:** {self.duration_seconds:.2f}s",
            f"**Pages Crawled:** {self.checked_pages}",
            f"**Links Checked:** {self.checked_links}",
            f"**Images Checked:** {self.checked_images}",
            f"**Issues Found:** {'Yes' if self.has_issues else 'No'}",
            "",
        ]
        if not self.has_issues:
            header.append("No issues detected. ✅")
            return "\n".join(header)

        sections = ["## Issues"]
        for idx, issue in enumerate(self.issues, start=1):
            lines = [
                f"### {idx}. {issue.kind.replace('_', ' ').title()}",
                issue.message,
            ]
            if issue.source:
                lines.append(f"- **Source:** {issue.source}")
            if issue.target:
                lines.append(f"- **Target:** {issue.target}")
            if issue.status_code is not None:
                lines.append(f"- **Status Code:** {issue.status_code}")
            sections.append("\n".join(lines))

        if self.has_warnings:
            sections.append("## Warnings")
            for idx, warning in enumerate(self.warnings, start=1):
                lines = [
                    f"### {idx}. {warning.kind.replace('_', ' ').title()}",
                    warning.message,
                ]
                if warning.source:
                    lines.append(f"- **Source:** {warning.source}")
                if warning.target:
                    lines.append(f"- **Target:** {warning.target}")
                if warning.status_code is not None:
                    lines.append(f"- **Status Code:** {warning.status_code}")
                sections.append("\n".join(lines))

        return "\n".join(header + sections)

    def to_html(self) -> str:
        generated_at = (
            dt.datetime.now(dt.timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )

        def render_stat_cards() -> str:
            stats = [
                ("Pages crawled", self.checked_pages),
                ("Links checked", self.checked_links),
                ("Images checked", self.checked_images),
                ("Duration (s)", round(self.duration_seconds, 2)),
            ]
            cards: List[str] = []
            for label, value in stats:
                cards.append(
                    textwrap.dedent(
                        f"""
                        <dl class="stat-card">
                          <dt>{escape(str(label))}</dt>
                          <dd>{escape(str(value))}</dd>
                        </dl>
                        """
                    ).strip()
                )
            return "\n".join(cards)

        def render_issue_list(title: str, items: List[Issue], empty_message: str) -> str:
            heading = escape(title)
            if not items:
                return textwrap.dedent(
                    f"""
                    <section>
                      <h2>{heading}</h2>
                      <p>{escape(empty_message)}</p>
                    </section>
                    """
                ).strip()

            entries: List[str] = []
            for idx, item in enumerate(items, start=1):
                detail_items: List[str] = []
                if item.source:
                    detail_items.append(f"<li><strong>Source:</strong> {escape(item.source)}</li>")
                if item.target:
                    detail_items.append(f"<li><strong>Target:</strong> {escape(item.target)}</li>")
                if item.status_code is not None:
                    detail_items.append(f"<li><strong>Status:</strong> {escape(str(item.status_code))}</li>")
                details_html = ""
                if detail_items:
                    details_html = "\n            <ul class=\"issue-details\">" + "".join(detail_items) + "</ul>"

                entries.append(
                    textwrap.dedent(
                        f"""
                        <li>
                          <article class="issue-card">
                            <header><h3>{idx}. {escape(item.kind.replace('_', ' ').title())}</h3></header>
                            <p>{escape(item.message)}</p>{details_html}
                          </article>
                        </li>
                        """
                    ).strip()
                )

            list_class = "issue-list" if title == "Issues" else "warning-list"
            items_html = "\n".join(entries)
            return textwrap.dedent(
                f"""
                <section>
                  <h2>{heading}</h2>
                  <ol class="{list_class}">
                    {items_html}
                  </ol>
                </section>
                """
            ).strip()

        status_badge = "pass" if not self.has_issues else "fail"
        status_label = "No issues detected" if status_badge == "pass" else "Issues detected"

        stats_html = render_stat_cards()
        issues_html = render_issue_list("Issues", self.issues, "No issues detected during this run. 🎉")
        warnings_html = render_issue_list("Warnings", self.warnings, "No warnings recorded in this run.")

        return textwrap.dedent(
            f"""
            <!DOCTYPE html>
            <html lang="en">
              <head>
                <meta charset="utf-8" />
                <meta name="viewport" content="width=device-width, initial-scale=1" />
                <title>Website check report — {escape(self.base_url)}</title>
                <style>
                  :root {{
                    color-scheme: light dark;
                    --bg: #f8f9fb;
                    --fg: #1f2933;
                    --muted: #52606d;
                    --card-bg: rgba(255, 255, 255, 0.9);
                    --border: rgba(15, 23, 42, 0.08);
                    --pass: #16a34a;
                    --fail: #dc2626;
                  }}
                  body {{
                    margin: 0;
                    font-family: "Inter", system-ui, -apple-system, "Segoe UI", sans-serif;
                    background: var(--bg);
                    color: var(--fg);
                    line-height: 1.6;
                  }}
                  header.hero {{
                    padding: 3rem 1.5rem 1rem;
                    text-align: center;
                  }}
                  header.hero h1 {{
                    margin-bottom: 0.5rem;
                    font-size: clamp(2rem, 5vw, 3rem);
                  }}
                  header.hero p {{
                    margin: 0.25rem 0;
                    color: var(--muted);
                  }}
                                    .status-badge {{
                                        display: inline-flex;
                                        align-items: center;
                    gap: 0.5rem;
                    padding: 0.35rem 0.85rem;
                    border-radius: 999px;
                    font-weight: 600;
                    letter-spacing: 0.04em;
                    text-transform: uppercase;
                    border: 1px solid var(--border);
                  }}
                  .status-badge.pass {{
                    color: var(--pass);
                    background: rgba(22, 163, 74, 0.12);
                  }}
                  .status-badge.fail {{
                    color: var(--fail);
                    background: rgba(220, 38, 38, 0.12);
                  }}
                  main {{
                    padding: 0 1.5rem 4rem;
                    margin: 0 auto;
                    max-width: 960px;
                  }}
                  .grid {{
                    display: grid;
                    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
                    gap: 1rem;
                  }}
                  .stat-card {{
                    background: var(--card-bg);
                    border: 1px solid var(--border);
                    border-radius: 1rem;
                    padding: 1.25rem;
                    box-shadow: 0 12px 24px rgba(15, 23, 42, 0.08);
                  }}
                  .stat-card dt {{
                    color: var(--muted);
                    font-size: 0.85rem;
                    text-transform: uppercase;
                    letter-spacing: 0.05em;
                    margin-bottom: 0.25rem;
                  }}
                  .stat-card dd {{
                    margin: 0;
                    font-size: 1.5rem;
                    font-weight: 700;
                  }}
                  section {{
                    margin: 2.5rem 0;
                  }}
                  ol.issue-list,
                  ol.warning-list {{
                    list-style: none;
                    padding: 0;
                    margin: 0;
                    display: grid;
                    gap: 1rem;
                  }}
                  .issue-card {{
                    background: var(--card-bg);
                    border: 1px solid var(--border);
                    border-radius: 1rem;
                    padding: 1rem 1.25rem;
                    box-shadow: 0 6px 16px rgba(15, 23, 42, 0.06);
                  }}
                  .issue-card h3 {{
                    margin: 0 0 0.35rem;
                  }}
                  .issue-details {{
                    margin: 0.85rem 0 0;
                    padding-left: 1.25rem;
                  }}
                  footer {{
                    text-align: center;
                    padding: 2rem 1rem 3rem;
                    color: var(--muted);
                    font-size: 0.85rem;
                  }}
                  @media (max-width: 540px) {{
                    main {{
                      padding: 0 1rem 3rem;
                    }}
                  }}
                </style>
              </head>
              <body>
                <header class="hero">
                  <p class="status-badge {status_badge}">{status_label}</p>
                  <h1>Website check report</h1>
                  <p><strong>Base URL:</strong> {escape(self.base_url)}</p>
                  <p><strong>Generated:</strong> {generated_at}</p>
                </header>
                <main>
                  <section>
                    <div class="grid">
                      {stats_html}
                    </div>
                  </section>
                  <section>
                    <h2>Summary</h2>
                    <p>The JSON, Markdown, and text summaries for this run are available for download:</p>
                    <ul>
                      <li><a href="website-check-report.json">Download JSON report</a></li>
                      <li><a href="website-check-report.md">Download Markdown report</a></li>
                      <li><a href="website-check-summary.txt">Download summary text</a></li>
                    </ul>
                  </section>
                  {issues_html}
                  {warnings_html}
                </main>
                <footer>
                  Published automatically from the website-check workflow.
                </footer>
              </body>
            </html>
            """
        ).strip()


class WebsiteChecker:
    def __init__(
        self,
        base_url: str,
        timeout: int = DEFAULT_TIMEOUT,
        internal_domains: Optional[Iterable[str]] = None,
    ) -> None:
        self.base_url = normalize_url(base_url)
        self.base_domain = urlparse(self.base_url).hostname or ""
        self.internal_domains = build_internal_domains(self.base_domain, internal_domains)
        self.timeout = timeout
        self.session = build_session()
        self.visited_pages: Dict[str, UrlCheckResult] = {}
        self.checked_links: Dict[str, UrlCheckResult] = {}
        self.checked_images: Dict[str, UrlCheckResult] = {}
        self.issues: List[Issue] = []
        self.warnings: List[Issue] = []

    def crawl(self) -> CrawlSummary:
        start_time = time.time()
        to_visit: Deque[str] = collections.deque([self.base_url])
        seen: Set[str] = set()

        while to_visit:
            current = to_visit.popleft()
            if current in seen:
                continue
            seen.add(current)

            page_result = self._fetch(current)
            self.visited_pages[current] = page_result
            if not page_result.ok:
                self.issues.append(
                    Issue(
                        kind="page_error",
                        message=f"Failed to load page {current}: {page_result.detail or 'HTTP error'}",
                        source=current,
                        status_code=page_result.status_code,
                    )
                )
                continue

            # Parse HTML to extract links and images
            html = page_result.detail or ""
            soup = BeautifulSoup(html, "html.parser")

            # Links
            for link_url in extract_links(current, soup):
                parsed = urlparse(link_url)
                if parsed.scheme not in ("http", "https"):
                    continue  # Skip non-http links

                domain = (parsed.hostname or "").lower()

                if link_url not in self.checked_links:
                    result = self._fetch(link_url, store_body=False)
                    self.checked_links[link_url] = result
                    if not result.ok:
                        if result.status_code == 999 and is_linkedin_domain(domain):
                            self.warnings.append(
                                Issue(
                                    kind="link_warning",
                                    message="LinkedIn returned HTTP 999 (likely bot protection). Please verify manually.",
                                    source=current,
                                    target=link_url,
                                    status_code=result.status_code,
                                )
                            )
                        else:
                            self.issues.append(
                                Issue(
                                    kind="link_error",
                                    message=f"Link failed to load: {result.detail or 'HTTP error'}",
                                    source=current,
                                    target=link_url,
                                    status_code=result.status_code,
                                )
                            )

                if domain in self.internal_domains and link_url not in seen:
                    to_visit.append(link_url)

            # Images
            for image_url in extract_images(current, soup):
                parsed_img = urlparse(image_url)
                if parsed_img.scheme not in ("http", "https"):
                    continue
                if image_url in self.checked_images:
                    continue
                result = self._fetch(image_url, store_body=False)
                self.checked_images[image_url] = result
                if not result.ok:
                    self.issues.append(
                        Issue(
                            kind="image_error",
                            message=f"Image failed to load: {result.detail or 'HTTP error'}",
                            source=current,
                            target=image_url,
                            status_code=result.status_code,
                        )
                    )
                    continue
                content_type = result.detail or ""
                if not content_type.startswith("image/"):
                    self.issues.append(
                        Issue(
                            kind="image_error",
                            message="Image URL did not return an image content type.",
                            source=current,
                            target=image_url,
                        )
                    )

        duration = time.time() - start_time
        link_errors_present = any(issue.kind == "link_error" for issue in self.issues)
        warnings = self.warnings if link_errors_present else []

        return CrawlSummary(
            base_url=self.base_url,
            checked_pages=len(self.visited_pages),
            checked_links=len(self.checked_links),
            checked_images=len(self.checked_images),
            duration_seconds=duration,
            issues=self.issues,
            warnings=warnings,
        )

    def _fetch(self, url: str, store_body: bool = True) -> UrlCheckResult:
        try:
            response = self.session.get(url, timeout=self.timeout)
            status_code = response.status_code
            ok = 200 <= status_code < 400
            if not ok:
                return UrlCheckResult(
                    url=url,
                    status_code=status_code,
                    ok=False,
                    detail=f"HTTP {status_code}",
                    elapsed=response.elapsed.total_seconds(),
                )
            if store_body:
                body = response.text
                return UrlCheckResult(
                    url=url,
                    status_code=status_code,
                    ok=True,
                    detail=body,
                    elapsed=response.elapsed.total_seconds(),
                )
            content_type = response.headers.get("Content-Type", "")
            return UrlCheckResult(
                url=url,
                status_code=status_code,
                ok=True,
                detail=content_type,
                elapsed=response.elapsed.total_seconds(),
            )
        except requests.RequestException as exc:  # pragma: no cover - network error paths
            return UrlCheckResult(url=url, status_code=None, ok=False, detail=str(exc))


def extract_links(base_url: str, soup: BeautifulSoup) -> Set[str]:
    links: Set[str] = set()
    for tag in soup.find_all("a", href=True):
        href = tag.get("href")
        if href:
            normalized = normalize_url(urljoin(base_url, href))
            if normalized:
                links.add(normalized)
    return links


def extract_images(base_url: str, soup: BeautifulSoup) -> Set[str]:
    images: Set[str] = set()
    for tag in soup.find_all("img", src=True):
        src = tag.get("src")
        if src:
            normalized = normalize_url(urljoin(base_url, src))
            if normalized:
                images.add(normalized)
    return images


def normalize_url(raw_url: str) -> str:
    if not raw_url:
        return ""
    parsed = urlparse(raw_url)
    if parsed.scheme not in ("http", "https"):
        return raw_url
    # Normalize path by removing fragments and resolving trailing slashes consistently
    path = parsed.path or "/"
    if parsed.path.endswith("/"):
        path = parsed.path
    else:
        path = parsed.path
    normalized = urlunparse((
        parsed.scheme,
        parsed.netloc.lower(),
        path,
        "",  # params not used
        parsed.query,
        "",  # remove fragment
    ))
    return normalized


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    session.max_redirects = 10
    return session


def build_internal_domains(
    base_domain: str,
    extra_domains: Optional[Iterable[str]] = None,
) -> Set[str]:
    domains: Set[str] = set()
    if base_domain:
        base = base_domain.lower()
        domains.add(base)
        if base.startswith("www."):
            domains.add(base[4:])
        else:
            domains.add(f"www.{base}")
    if extra_domains:
        for domain in extra_domains:
            cleaned = (domain or "").strip().lower()
            if cleaned:
                domains.add(cleaned)
    return {domain for domain in domains if domain}


def is_linkedin_domain(domain: str) -> bool:
    domain = (domain or "").lower()
    return domain.endswith("linkedin.com")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Crawl a website to verify pages, links, and images.")
    parser.add_argument("--base-url", dest="base_url", default=os.environ.get("BASE_URL"), help="Base URL to crawl.")
    parser.add_argument(
        "--json-output",
        dest="json_output",
        default=os.environ.get("JSON_OUTPUT", "website-check-report.json"),
        help="Path to write JSON report (default: website-check-report.json)",
    )
    parser.add_argument(
        "--markdown-output",
        dest="markdown_output",
        default=os.environ.get("MARKDOWN_OUTPUT", "website-check-report.md"),
        help="Path to write Markdown report (default: website-check-report.md)",
    )
    parser.add_argument(
        "--html-output",
        dest="html_output",
        default=os.environ.get("HTML_OUTPUT", "website-check-report.html"),
        help="Path to write HTML report (default: website-check-report.html)",
    )
    parser.add_argument(
        "--timeout",
        dest="timeout",
        type=int,
        default=int(os.environ.get("REQUEST_TIMEOUT", DEFAULT_TIMEOUT)),
        help="Timeout for HTTP requests in seconds (default: 15)",
    )
    parser.add_argument(
        "--internal-domains",
        dest="internal_domains",
        default=os.environ.get("INTERNAL_DOMAINS"),
        help="Comma-separated list of additional domains to treat as internal for crawling.",
    )
    args = parser.parse_args(argv)
    if not args.base_url:
        parser.error("--base-url is required (or set BASE_URL environment variable)")
    return args


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    internal_domains = None
    if args.internal_domains:
        internal_domains = [domain.strip() for domain in args.internal_domains.split(",") if domain.strip()]

    checker = WebsiteChecker(args.base_url, timeout=args.timeout, internal_domains=internal_domains)
    summary = checker.crawl()

    # Write reports
    with open(args.json_output, "w", encoding="utf-8") as fh:
        json.dump(summary.to_json(), fh, indent=2)
    with open(args.markdown_output, "w", encoding="utf-8") as fh:
        fh.write(summary.to_markdown())
    with open(args.html_output, "w", encoding="utf-8") as fh:
        fh.write(summary.to_html())

    # Print concise summary to stdout for logs
    print(json.dumps(summary.to_json()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
