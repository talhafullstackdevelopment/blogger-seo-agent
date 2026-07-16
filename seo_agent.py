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

REPORTS_DIR = Path("reports")
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"


# ---------------------------------------------------------------------------
# Blogger API auth + client
# ---------------------------------------------------------------------------

def get_blogger_service():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token_json = os.environ["GOOGLE_TOKEN"]
    creds_info = json.loads(token_json)
    creds = Credentials.from_authorized_user_info(
        creds_info,
        scopes=["https://www.googleapis.com/auth/blogger"],
    )

    if creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request
        creds.refresh(Request())

    return build("blogger", "v3", credentials=creds)


def list_all_posts(service, blog_id):
    posts = []
    request = service.posts().list(blogId=blog_id, fetchBodies=True, maxResults=500, status="live")
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

def audit_and_fix_post(service, blog_id, post):
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
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    blog_id = os.environ["BLOGGER_BLOG_ID"]
    service = get_blogger_service()

    posts = list_all_posts(service, blog_id)
    print(f"Found {len(posts)} live posts on blog {blog_id}")

    results = []
    for post in posts:
        try:
            results.append(audit_and_fix_post(service, blog_id, post))
        except Exception as e:
            print(f"[error] skipping post {post.get('url')}: {e}")

    REPORTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.datetime.utcnow().strftime("%Y-%m-%d_%H%M")
    json_path = REPORTS_DIR / f"report_{stamp}.json"
    md_path = REPORTS_DIR / f"report_{stamp}.md"

    json_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

    lines = [f"# SEO/GEO/AEO Agent Report — {stamp} UTC", ""]
    for r in results:
        s = r["scores_before"]
        lines.append(f"## {r['title']}")
        lines.append(f"[{r['url']}]({r['url']})")
        lines.append("")
        lines.append(
            f"- Overall: **{s['overall']}** | Technical SEO: {s['technical_seo']} | "
            f"Content: {s['content_depth']} | GEO: {s['geo_readiness']} | Trust: {s['trust_authority']}"
        )
        lines.append(f"- Fixes applied: {r['fixes_applied'] or 'none'}")
        lines.append(f"- Remaining recommendations: {r['recommendations']}")
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"\nReport written: {json_path}, {md_path}")


if __name__ == "__main__":
    main()
