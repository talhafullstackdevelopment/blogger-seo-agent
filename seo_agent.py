#!/usr/bin/env python3
"""
Blogger SEO + GEO + AEO Agent
==============================
Runs on a schedule (GitHub Actions). For every post on a Blogger site:
  1. Audits it (Technical SEO, Content Depth, GEO Readiness, Trust/Authority)
     using audit_core.py (same scoring engine as the SEO/GEO audit skill).
  2. Auto-fixes what's safely fixable via the Blogger API:
       - missing/too-long meta description (searchDescription)
       - missing labels/tags
       - missing alt text on images
       - missing FAQPage JSON-LD schema (AEO) -- generated with Gemini if
         GEMINI_API_KEY is set, else a simple template fallback
       - missing "quick answer" TL;DR paragraph at top of post (AEO)
  3. Writes a JSON + Markdown report to reports/ and pushes it back to git
     (handled by the GitHub Actions workflow, not this script).

Auth:
  Blogger API writes need OAuth2 (same client_secret.json / token.pickle
  pattern as your tiny-genius-agent YouTube pipeline). Reads (listing posts)
  can use an API key alone, but since we need to WRITE fixes, OAuth is
  required end-to-end here.

Env vars expected (set as GitHub Actions secrets):
  BLOGGER_BLOG_ID        - numeric Blogger blog ID (see README for how to get it)
  GOOGLE_CLIENT_SECRET   - contents of client_secret.json (as a string)
  GOOGLE_TOKEN           - contents of token.json / pickle-derived refresh token (as JSON string)
  GEMINI_API_KEY         - optional, enables AI-generated meta descriptions/FAQs
  DRY_RUN                - "true" to only audit + report, no writes (default: false)
"""

import os
import re
import json
import base64
import datetime
from pathlib import Path

from audit_core import analyze_html, recommendations, fetch as fetch_url
from performance_data import (
    get_search_console_service,
    get_gsc_data_for_url,
    get_pagespeed_data,
    find_broken_links,
    compute_priority_score,
)

REPORTS_DIR = Path("reports")
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
SITE_URL = os.environ.get("SITE_URL", "")  # e.g. https://documentconvertfree.blogspot.com/
PAGESPEED_API_KEY = os.environ.get("PAGESPEED_API_KEY", "")
ENABLE_PAGESPEED = os.environ.get("ENABLE_PAGESPEED", "true").lower() == "true"
ENABLE_GSC = os.environ.get("ENABLE_GSC", "true").lower() == "true"
ENABLE_BROKEN_LINKS = os.environ.get("ENABLE_BROKEN_LINKS", "true").lower() == "true"


# ---------------------------------------------------------------------------
# Blogger API auth + client
# ---------------------------------------------------------------------------

def get_credentials():
    """Shared OAuth creds for Blogger + Search Console. The token JSON must
    have been generated with BOTH scopes (see get_token.py)."""
    from google.oauth2.credentials import Credentials

    token_json = os.environ["GOOGLE_TOKEN"]
    creds_info = json.loads(token_json)
    creds = Credentials.from_authorized_user_info(
        creds_info,
        scopes=[
            "https://www.googleapis.com/auth/blogger",
            "https://www.googleapis.com/auth/webmasters.readonly",
        ],
    )

    if creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request
        creds.refresh(Request())

    return creds


def get_blogger_service(creds):
    from googleapiclient.discovery import build
    return build("blogger", "v3", credentials=creds)


def list_all_posts(service, blog_id):
    posts = []
    request = service.posts().list(blogId=blog_id, fetchBodies=True, maxResults=500, status="LIVE")
    while request is not None:
        response = request.execute()
        posts.extend(response.get("items", []))
        request = service.posts().list_next(request, response)
    return posts


# ---------------------------------------------------------------------------
# AEO / GEO content helpers
# ---------------------------------------------------------------------------

def gemini_generate(prompt, max_tokens=400):
    """Optional: use free-tier Gemini to generate meta descriptions / FAQs.
    Falls back to None if no API key is set, caller must handle that."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    import urllib.request

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.0-flash:generateContent?key={api_key}"
    )
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.4},
    }).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print(f"  [gemini] skipped ({e})")
        return None


def strip_html(html):
    return re.sub(r"<[^>]+>", " ", html)


def build_meta_description(title, content_html):
    prompt = (
        f"Write a compelling SEO meta description (max 155 characters, plain text, "
        f"no quotes) for a blog post titled '{title}'. Content summary: "
        f"{strip_html(content_html)[:800]}"
    )
    generated = gemini_generate(prompt, max_tokens=100)
    if generated:
        return generated[:157].strip()
    # fallback: first sentence-ish chunk of body text
    text = strip_html(content_html).strip()
    text = re.sub(r"\s+", " ", text)
    return (text[:152] + "...") if len(text) > 155 else text


def build_faq_schema(title, content_html):
    prompt = (
        f"Based on this blog post titled '{title}', write exactly 3 short FAQ "
        f"question-answer pairs a reader might ask (answers 1-2 sentences each). "
        f"Content: {strip_html(content_html)[:1000]}\n\n"
        f"Respond ONLY as JSON: [{{\"q\": \"...\", \"a\": \"...\"}}, ...]"
    )
    generated = gemini_generate(prompt, max_tokens=500)
    faqs = None
    if generated:
        try:
            cleaned = generated.replace("```json", "").replace("```", "").strip()
            faqs = json.loads(cleaned)
        except Exception:
            faqs = None
    if not faqs:
        faqs = [
            {"q": f"What is {title}?", "a": f"{title} is a free tool explained on this page."},
            {"q": "Is it free to use?", "a": "Yes, this tool is completely free with no registration required."},
            {"q": "Do I need to install anything?", "a": "No installation needed — it works directly in your browser."},
        ]

    entities = []
    for item in faqs[:5]:
        entities.append({
            "@type": "Question",
            "name": item["q"],
            "acceptedAnswer": {"@type": "Answer", "text": item["a"]},
        })
    schema = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": entities,
    }
    script_tag = f'<script type="application/ld+json">{json.dumps(schema)}</script>'
    return script_tag, faqs


def add_missing_alt_text(content_html, title):
    """Give every <img> without alt= a reasonable fallback alt attribute."""
    def fix(match):
        tag = match.group(0)
        if re.search(r'alt\s*=\s*"[^"]+"', tag) or re.search(r"alt\s*=\s*'[^']+'", tag):
            return tag
        return tag[:-1].rstrip("/") + f' alt="{title}"' + (" />" if tag.endswith("/>") else ">")

    return re.sub(r"<img\b[^>]*/?>", fix, content_html)


def has_quick_answer_block(content_html):
    return "quick-answer-tldr" in content_html


def build_quick_answer_block(title, content_html):
    prompt = (
        f"In 1-2 plain sentences, give the single most direct 'quick answer' "
        f"summary of this blog post titled '{title}', written so an AI answer "
        f"engine (like ChatGPT or Google AI Overviews) could quote it directly. "
        f"Content: {strip_html(content_html)[:800]}"
    )
    answer = gemini_generate(prompt, max_tokens=120)
    if not answer:
        answer = strip_html(content_html).strip()
        answer = re.sub(r"\s+", " ", answer)[:200]
    return f'<p class="quick-answer-tldr"><strong>Quick answer:</strong> {answer}</p>'


# ---------------------------------------------------------------------------
# Per-post pipeline
# ---------------------------------------------------------------------------

def audit_and_fix_post(service, blog_id, post, gsc_service=None):
    url = post["url"]
    title = post["title"]
    post_id = post["id"]
    content_html = post.get("content", "")

    print(f"\n--- Auditing: {title} ({url})")

    try:
        live_html = fetch_url(url)
    except Exception as e:
        print(f"  could not fetch live page: {e}")
        live_html = f"<html><head><title>{title}</title></head><body>{content_html}</body></html>"

    report_before = analyze_html(live_html, url)
    recs = recommendations(report_before)

    # --- Real performance data (Search Console / PageSpeed / broken links) ---
    gsc_data = {"clicks": 0, "impressions": 0, "ctr": 0.0, "position": None, "has_data": False}
    if ENABLE_GSC and gsc_service and SITE_URL:
        gsc_data = get_gsc_data_for_url(gsc_service, SITE_URL, url)
        print(f"  [gsc] clicks={gsc_data['clicks']} impressions={gsc_data['impressions']} "
              f"position={gsc_data['position']}")

    pagespeed_data = None
    if ENABLE_PAGESPEED:
        pagespeed_data = get_pagespeed_data(url, api_key=PAGESPEED_API_KEY or None)
        if pagespeed_data:
            print(f"  [pagespeed] score={pagespeed_data['performance_score']} "
                  f"LCP={pagespeed_data['lcp']} CLS={pagespeed_data['cls']}")

    broken_links = {"total_checked": 0, "broken": [], "broken_count": 0}
    if ENABLE_BROKEN_LINKS:
        broken_links = find_broken_links(live_html)
        if broken_links["broken_count"]:
            print(f"  [links] {broken_links['broken_count']} broken link(s) found")

    priority_score = compute_priority_score(
        report_before["scores"], gsc_data, pagespeed_data or {}, broken_links
    )

    fixes_applied = []
    new_content = content_html
    update_body = {}

    # 1. Meta description
    if report_before["meta_description_length"] == 0 or report_before["meta_description_length"] > 160:
        desc = build_meta_description(title, content_html)
        update_body["searchDescription"] = desc
        fixes_applied.append(f"meta description set ({len(desc)} chars)")

    # 2. Labels
    if not post.get("labels"):
        # cheap heuristic label from title's first significant word
        guess_label = title.split()[0] if title else "General"
        update_body["labels"] = [guess_label, "Free Tools"]
        fixes_applied.append(f"labels added: {update_body['labels']}")

    # 3. Alt text on images
    fixed_alt = add_missing_alt_text(new_content, title)
    if fixed_alt != new_content:
        new_content = fixed_alt
        fixes_applied.append("added missing image alt text")

    # 4. FAQ schema (AEO/GEO)
    if not report_before["has_faq_or_howto_schema"]:
        faq_script, _ = build_faq_schema(title, content_html)
        new_content = new_content + "\n" + faq_script
        fixes_applied.append("injected FAQPage JSON-LD schema")

    # 5. Quick-answer TL;DR block (AEO)
    if not has_quick_answer_block(new_content):
        qa_block = build_quick_answer_block(title, content_html)
        new_content = qa_block + "\n" + new_content
        fixes_applied.append("added quick-answer TL;DR block")

    if new_content != content_html:
        update_body["content"] = new_content

    if update_body and not DRY_RUN:
        try:
            service.posts().patch(blogId=blog_id, postId=post_id, body=update_body).execute()
            print(f"  applied fixes: {fixes_applied}")
        except Exception as e:
            print(f"  [error] failed to patch post: {e}")
            fixes_applied.append(f"FAILED to apply: {e}")
    elif update_body and DRY_RUN:
        print(f"  [dry-run] would apply: {fixes_applied}")
    else:
        print("  no fixes needed")

    return {
        "url": url,
        "title": title,
        "scores_before": report_before["scores"],
        "recommendations": recs,
        "fixes_applied": fixes_applied,
        "dry_run": DRY_RUN,
        "search_console": gsc_data,
        "pagespeed": pagespeed_data,
        "broken_links": broken_links,
        "priority_score": priority_score,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    blog_id = os.environ["BLOGGER_BLOG_ID"]
    creds = get_credentials()
    service = get_blogger_service(creds)

    gsc_service = None
    if ENABLE_GSC and SITE_URL:
        try:
            gsc_service = get_search_console_service(creds)
        except Exception as e:
            print(f"[warn] Search Console unavailable, continuing without it: {e}")
    elif ENABLE_GSC and not SITE_URL:
        print("[warn] ENABLE_GSC is true but SITE_URL is not set -- skipping Search Console data")

    posts = list_all_posts(service, blog_id)
    print(f"Found {len(posts)} live posts on blog {blog_id}")

    results = []
    for post in posts:
        try:
            results.append(audit_and_fix_post(service, blog_id, post, gsc_service=gsc_service))
        except Exception as e:
            print(f"[error] skipping post {post.get('url')}: {e}")

    # Highest priority (most worth fixing) first
    results.sort(key=lambda r: r.get("priority_score", 0), reverse=True)

    REPORTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.datetime.utcnow().strftime("%Y-%m-%d_%H%M")
    json_path = REPORTS_DIR / f"report_{stamp}.json"
    md_path = REPORTS_DIR / f"report_{stamp}.md"

    json_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

    lines = [f"# SEO/GEO/AEO Agent Report — {stamp} UTC", ""]
    lines.append("Posts below are sorted by **priority score** (highest = most worth fixing).")
    lines.append("")
    for r in results:
        s = r["scores_before"]
        gsc = r.get("search_console") or {}
        ps = r.get("pagespeed") or {}
        links = r.get("broken_links") or {}
        lines.append(f"## {r['title']}  (priority: {r.get('priority_score', 'n/a')})")
        lines.append(f"[{r['url']}]({r['url']})")
        lines.append("")
        lines.append(
            f"- On-page: Overall **{s['overall']}** | Technical: {s['technical_seo']} | "
            f"Content: {s['content_depth']} | GEO: {s['geo_readiness']} | Trust: {s['trust_authority']}"
        )
        if gsc.get("has_data"):
            lines.append(
                f"- Search Console (28d): {gsc['clicks']} clicks | {gsc['impressions']} impressions | "
                f"{gsc['ctr']}% CTR | avg position {gsc['position']}"
            )
        else:
            lines.append("- Search Console: no data yet (new or very low-traffic page)")
        if ps:
            lines.append(
                f"- PageSpeed (mobile): score {ps.get('performance_score', 'n/a')} | "
                f"LCP {ps.get('lcp', 'n/a')} | CLS {ps.get('cls', 'n/a')} | TBT {ps.get('inp_or_tbt', 'n/a')}"
            )
        if links.get("total_checked"):
            status = f"{links['broken_count']} broken" if links["broken_count"] else "all OK"
            lines.append(f"- Links checked: {links['total_checked']} ({status})")
            if links["broken"]:
                for b in links["broken"]:
                    lines.append(f"  - broken: {b}")
        lines.append(f"- Fixes applied: {r['fixes_applied'] or 'none'}")
        lines.append(f"- Remaining recommendations: {r['recommendations']}")
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"\nReport written: {json_path}, {md_path}")


if __name__ == "__main__":
    main()
