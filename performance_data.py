#!/usr/bin/env python3
"""
Performance data collectors for the SEO/GEO/AEO agent.

Adds three data sources on top of the on-page audit (audit_core.py):
  1. Google Search Console  -- real clicks, impressions, CTR, avg position
  2. Google PageSpeed Insights -- Core Web Vitals + performance score (free API)
  3. Broken link checker -- HEAD-requests every link found on the page

These are used to (a) enrich the report with real-world data, and
(b) compute a priority score so low-performing posts get fixed first.
"""

import os
import re
import json
import time
import urllib.request
import urllib.error
import urllib.parse

UA = "Mozilla/5.0 (compatible; SEOGeoAuditor/1.0)"


# ---------------------------------------------------------------------------
# 1. Google Search Console
# ---------------------------------------------------------------------------

def get_search_console_service(creds):
    """Reuses the same OAuth creds as Blogger (needs the extra
    webmasters.readonly scope added to the token)."""
    from googleapiclient.discovery import build
    return build("searchconsole", "v1", credentials=creds)


def get_gsc_data_for_url(service, site_url, page_url, days=28):
    """Returns clicks, impressions, ctr, position for a specific page
    over the last `days` days. Falls back to zeros if no data yet
    (common for new/low-traffic pages)."""
    import datetime
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days)

    body = {
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "dimensions": ["page"],
        "dimensionFilterGroups": [{
            "filters": [{"dimension": "page", "operator": "equals", "expression": page_url}]
        }],
        "rowLimit": 1,
    }
    try:
        response = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
        rows = response.get("rows", [])
        if not rows:
            return {"clicks": 0, "impressions": 0, "ctr": 0.0, "position": None, "has_data": False}
        row = rows[0]
        return {
            "clicks": row.get("clicks", 0),
            "impressions": row.get("impressions", 0),
            "ctr": round(row.get("ctr", 0) * 100, 2),
            "position": round(row.get("position", 0), 1),
            "has_data": True,
        }
    except Exception as e:
        print(f"  [gsc] could not fetch data for {page_url}: {e}")
        return {"clicks": 0, "impressions": 0, "ctr": 0.0, "position": None, "has_data": False}


# ---------------------------------------------------------------------------
# 2. Google PageSpeed Insights (Core Web Vitals)
# ---------------------------------------------------------------------------

def get_pagespeed_data(url, api_key=None, strategy="mobile", timeout=30):
    """Free API, no OAuth needed. Without an API key you get a low shared
    rate limit; set PAGESPEED_API_KEY (also free) for reliable results."""
    params = f"url={urllib.parse.quote(url)}&strategy={strategy}&category=performance"
    if api_key:
        params += f"&key={api_key}"
    api_url = f"https://www.googleapis.com/pagespeedonline/v5/runPagespeed?{params}"

    req = urllib.request.Request(api_url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"  [pagespeed] failed for {url}: {e}")
        return None

    try:
        lighthouse = data["lighthouseResult"]
        categories = lighthouse["categories"]
        audits = lighthouse["audits"]
        perf_score = round(categories["performance"]["score"] * 100)

        def metric(key):
            a = audits.get(key, {})
            return a.get("displayValue", "n/a")

        return {
            "performance_score": perf_score,
            "lcp": metric("largest-contentful-paint"),       # Largest Contentful Paint
            "cls": metric("cumulative-layout-shift"),         # Cumulative Layout Shift
            "inp_or_tbt": metric("total-blocking-time"),      # proxy for INP
            "fcp": metric("first-contentful-paint"),
        }
    except (KeyError, TypeError) as e:
        print(f"  [pagespeed] unexpected response shape for {url}: {e}")
        return None


# ---------------------------------------------------------------------------
# 3. Broken link checker
# ---------------------------------------------------------------------------

def extract_links(html):
    return list(set(re.findall(r'href=["\'](https?://[^"\']+)["\']', html)))


def check_link(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": UA}, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status < 400
    except urllib.error.HTTPError as e:
        # Some servers reject HEAD but are fine with GET; retry once.
        if e.code == 405:
            try:
                req2 = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req2, timeout=timeout) as resp2:
                    return resp2.status < 400
            except Exception:
                return False
        return e.code < 400
    except Exception:
        return False


def find_broken_links(html, max_links=25, delay=0.3):
    links = extract_links(html)[:max_links]
    broken = []
    for link in links:
        ok = check_link(link)
        if not ok:
            broken.append(link)
        time.sleep(delay)  # be polite, avoid hammering external hosts
    return {"total_checked": len(links), "broken": broken, "broken_count": len(broken)}


# ---------------------------------------------------------------------------
# Priority scoring
# ---------------------------------------------------------------------------

def compute_priority_score(onpage_scores, gsc_data, pagespeed_data, broken_links):
    """Higher score = fix this post sooner.
    Weighs: low on-page score, real search impressions with poor position
    (i.e. 'almost ranking' pages are highest value to fix), poor page speed,
    and broken links."""
    score = 0

    # On-page weakness (0-100 scale, lower page score = more points)
    score += (100 - onpage_scores.get("overall", 100)) * 0.4

    # GSC: pages with impressions but poor position are high-value targets
    if gsc_data.get("has_data"):
        impressions = gsc_data.get("impressions", 0)
        position = gsc_data.get("position") or 100
        if impressions > 0 and position > 10:
            score += min(30, impressions / 10) + min(20, position / 5)
        elif impressions == 0:
            score += 10  # not being seen at all -- worth checking indexing/content

    # PageSpeed
    if pagespeed_data:
        score += (100 - pagespeed_data.get("performance_score", 100)) * 0.2

    # Broken links are a hard penalty
    score += broken_links.get("broken_count", 0) * 5

    return round(score, 1)
