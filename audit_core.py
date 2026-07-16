#!/usr/bin/env python3
"""
SEO + GEO site auditor.

Fetches one or two URLs (client, and optionally a competitor), extracts
on-page SEO and Generative Engine Optimization (GEO) signals, scores them,
and prints a JSON report to stdout.

Usage:
    python audit.py <client_url> [competitor_url]
    python audit.py <client_url> --html <path_to_saved_html>   # offline mode

No third-party dependencies required (stdlib only), so it runs anywhere
Python 3 runs, including sandboxes without pip access.
"""

import sys
import json
import re
import argparse
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urlparse

UA = "Mozilla/5.0 (compatible; SEOGeoAuditor/1.0; +https://www.anthropic.com)"


def fetch(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


class PageParser(HTMLParser):
    """Minimal stdlib HTML parser that pulls the signals we need."""

    def __init__(self, base_url):
        super().__init__()
        self.base_host = urlparse(base_url).netloc
        self.title = ""
        self.meta_description = ""
        self.meta_robots = ""
        self.has_canonical = False
        self.has_viewport = False
        self.h1 = 0
        self.h2 = 0
        self.h3 = 0
        self.img_total = 0
        self.img_with_alt = 0
        self.internal_links = 0
        self.external_links = 0
        self.json_ld_blocks = []
        self.has_author_tag = False
        self.has_date_tag = False
        self.headings_text = []

        self._in_title = False
        self._in_heading = None
        self._current_heading_text = ""
        self._in_json_ld = False
        self._json_ld_buffer = ""
        self._text_parts = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = (attrs_dict.get("name") or "").lower()
            prop = (attrs_dict.get("property") or "").lower()
            if name == "description":
                self.meta_description = attrs_dict.get("content", "").strip()
            elif name == "robots":
                self.meta_robots = attrs_dict.get("content", "").strip()
            elif name == "viewport":
                self.has_viewport = True
            elif name == "author":
                self.has_author_tag = True
            elif prop == "article:published_time":
                self.has_date_tag = True
        elif tag == "link":
            if (attrs_dict.get("rel") or "").lower() == "canonical":
                self.has_canonical = True
        elif tag in ("h1", "h2", "h3"):
            setattr(self, tag, getattr(self, tag) + 1)
            self._in_heading = tag
            self._current_heading_text = ""
        elif tag == "img":
            self.img_total += 1
            if (attrs_dict.get("alt") or "").strip():
                self.img_with_alt += 1
        elif tag == "a":
            href = attrs_dict.get("href", "")
            if href.startswith("http://") or href.startswith("https://"):
                host = urlparse(href).netloc
                if host == self.base_host:
                    self.internal_links += 1
                else:
                    self.external_links += 1
            elif href.startswith("/") or href.startswith("#"):
                self.internal_links += 1
        elif tag == "time":
            self.has_date_tag = True
        elif tag == "script":
            if (attrs_dict.get("type") or "").lower() == "application/ld+json":
                self._in_json_ld = True
                self._json_ld_buffer = ""
        elif tag in ("author",) or attrs_dict.get("itemprop") == "author":
            self.has_author_tag = True
        elif attrs_dict.get("itemprop") == "datePublished":
            self.has_date_tag = True
        elif attrs_dict.get("class") and "author" in (attrs_dict.get("class") or "").lower():
            self.has_author_tag = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in ("h1", "h2", "h3") and self._in_heading == tag:
            self.headings_text.append(self._current_heading_text.strip())
            self._in_heading = None
        elif tag == "script" and self._in_json_ld:
            self.json_ld_blocks.append(self._json_ld_buffer)
            self._in_json_ld = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._in_heading:
            self._current_heading_text += data
        if self._in_json_ld:
            self._json_ld_buffer += data
        # crude body text accumulation for word count / stat density
        stripped = data.strip()
        if stripped:
            self._text_parts.append(stripped)

    @property
    def body_text(self):
        return " ".join(self._text_parts)


def analyze_html(html, url):
    parser = PageParser(url)
    try:
        parser.feed(html)
    except Exception:
        pass

    text = parser.body_text
    word_count = len(text.split())

    title_len = len(parser.title.strip())
    meta_desc_len = len(parser.meta_description.strip())
    noindex = bool(re.search(r"noindex", parser.meta_robots, re.I))
    https = url.lower().startswith("https://")

    alt_coverage = (
        round((parser.img_with_alt / parser.img_total) * 100)
        if parser.img_total else 100
    )

    has_schema = len(parser.json_ld_blocks) > 0
    has_faq_schema = any(
        re.search(r"FAQPage|QAPage|HowTo", block, re.I)
        for block in parser.json_ld_blocks
    )

    question_headings = sum(1 for h in parser.headings_text if h.strip().endswith("?"))
    stat_density = len(re.findall(r"\b\d{1,3}(\.\d+)?%|\b\d{4}\b|\$\d", text))

    has_author = parser.has_author_tag
    has_date = parser.has_date_tag

    # --- scoring (mirrors the interactive analyzer tool) ---
    technical = 0
    technical += 20 if 0 < title_len <= 60 else (10 if title_len > 0 else 0)
    technical += 20 if 0 < meta_desc_len <= 160 else (10 if meta_desc_len > 0 else 0)
    technical += 15 if parser.has_canonical else 0
    technical += 15 if not noindex else 0
    technical += 15 if parser.has_viewport else 0
    technical += 15 if https else 0
    technical = min(100, technical)

    content = 0
    content += min(30, round((word_count / 800) * 30))
    content += 15 if parser.h1 == 1 else 0
    content += 15 if parser.h2 >= 2 else (8 if parser.h2 > 0 else 0)
    content += min(20, round((alt_coverage / 80) * 20))
    content += min(10, round((parser.internal_links / 3) * 10))
    content += 10 if parser.external_links >= 1 else 0
    content = min(100, round(content))

    geo = 0
    geo += 25 if has_schema else 0
    geo += 20 if has_faq_schema else 0
    geo += 15 if question_headings > 0 else 0
    geo += min(20, round((stat_density / 5) * 20))
    geo += 10 if has_author else 0
    geo += 10 if has_date else 0
    geo = min(100, round(geo))

    trust = 0
    trust += 25 if has_author else 0
    trust += 25 if has_date else 0
    trust += min(25, round((parser.external_links / 2) * 25))
    trust += 25 if https else 0
    trust = min(100, round(trust))

    overall = round((technical + content + geo + trust) / 4)

    return {
        "url": url,
        "word_count": word_count,
        "title": parser.title.strip(),
        "title_length": title_len,
        "meta_description": parser.meta_description.strip(),
        "meta_description_length": meta_desc_len,
        "canonical_tag": parser.has_canonical,
        "noindex": noindex,
        "viewport_tag": parser.has_viewport,
        "https": https,
        "h1_count": parser.h1,
        "h2_count": parser.h2,
        "h3_count": parser.h3,
        "images_total": parser.img_total,
        "alt_text_coverage_pct": alt_coverage,
        "internal_links": parser.internal_links,
        "external_links": parser.external_links,
        "has_schema_markup": has_schema,
        "has_faq_or_howto_schema": has_faq_schema,
        "question_style_headings": question_headings,
        "cited_stat_count": stat_density,
        "has_author_byline": has_author,
        "has_published_date": has_date,
        "scores": {
            "technical_seo": technical,
            "content_depth": content,
            "geo_readiness": geo,
            "trust_authority": trust,
            "overall": overall,
        },
    }


def recommendations(client, competitor=None):
    recs = []
    c = client
    if c["title_length"] == 0:
        recs.append("Add a <title> tag \u2014 currently missing.")
    elif c["title_length"] > 60:
        recs.append("Shorten the title tag to under 60 characters.")
    if c["meta_description_length"] == 0:
        recs.append("Add a meta description (140\u2013160 characters).")
    if not c["canonical_tag"]:
        recs.append("Add a canonical tag.")
    if not c["viewport_tag"]:
        recs.append("Add a mobile viewport meta tag.")
    if c["h1_count"] != 1:
        recs.append("Use exactly one H1 per page.")
    if c["alt_text_coverage_pct"] < 80:
        recs.append(f"Add alt text to images \u2014 only {c['alt_text_coverage_pct']}% covered.")
    if not c["has_schema_markup"]:
        recs.append("Add schema.org structured data (JSON-LD).")
    if c["question_style_headings"] == 0:
        recs.append("Add question-style subheadings for better AI answer-engine visibility.")
    if c["cited_stat_count"] < 3:
        recs.append("Include more concrete numbers/stats \u2014 citable facts help GEO.")
    if not c["has_author_byline"]:
        recs.append("Add a visible author byline (E-E-A-T signal).")
    if not c["has_published_date"]:
        recs.append("Show a published/last-updated date.")
    if competitor:
        p = competitor
        if not c["has_faq_or_howto_schema"] and p["has_faq_or_howto_schema"]:
            recs.append("Competitor has FAQ/HowTo schema and you don't \u2014 strong GEO gap.")
        if c["scores"]["overall"] < p["scores"]["overall"]:
            diff = p["scores"]["overall"] - c["scores"]["overall"]
            recs.append(f"Competitor scores {diff} points higher overall \u2014 close the gaps above.")
    if not recs:
        recs.append("No major gaps found on the checks run here.")
    return recs


def main():
    ap = argparse.ArgumentParser(description="SEO + GEO page auditor")
    ap.add_argument("client_url", help="Client page URL")
    ap.add_argument("competitor_url", nargs="?", help="Optional competitor page URL")
    ap.add_argument("--client-html", help="Path to saved HTML for client (offline mode)")
    ap.add_argument("--competitor-html", help="Path to saved HTML for competitor (offline mode)")
    args = ap.parse_args()

    if args.client_html:
        with open(args.client_html, encoding="utf-8", errors="replace") as f:
            client_html = f.read()
    else:
        client_html = fetch(args.client_url)
    client_report = analyze_html(client_html, args.client_url)

    competitor_report = None
    if args.competitor_url or args.competitor_html:
        if args.competitor_html:
            with open(args.competitor_html, encoding="utf-8", errors="replace") as f:
                comp_html = f.read()
        else:
            comp_html = fetch(args.competitor_url)
        competitor_report = analyze_html(comp_html, args.competitor_url or args.client_url)

    output = {
        "client": client_report,
        "competitor": competitor_report,
        "recommendations": recommendations(client_report, competitor_report),
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
