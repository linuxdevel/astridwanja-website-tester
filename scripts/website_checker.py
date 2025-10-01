"""Website checker for verifying pages, links, and images on a target site.

Usage:
    python scripts/website_checker.py \
        --base-url https://example.com \
    [--internal-domains example.com,www.example.com] \
        --json-output report.json \
        --markdown-output report.md

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
import json
import os
import sys
import time
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
            f"# Website check report",
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

    # Print concise summary to stdout for logs
    print(json.dumps(summary.to_json()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
