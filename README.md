# Blogger SEO + GEO + AEO Agent (v2 — full SEO suite)

Yeh agent aapki Blogger site ke har post ko har hafte automatically audit +
fix karta hai, **aur ab real Google data bhi use karta hai** — sirf guessing
nahi, asal traffic/ranking ke hisab se decide karta hai kaunsi post pehle
theek karni hai.

## Kya naya hai (v2)

1. **Google Search Console** — har post ke real clicks, impressions, CTR,
   average ranking position (pichle 28 din ka data)
2. **Google PageSpeed Insights** — Core Web Vitals (LCP, CLS, TBT) + speed
   score, free Google API se
3. **Broken link checker** — har post ke internal/external links check
   karta hai, dead links flag karta hai
4. **Priority score** — in sab cheezon ko mila kar decide karta hai kaunsi
   post sabse zyada dhyan maangti hai (report mein sabse upar dikhegi)

Purana on-page audit (title, meta, headings, alt text, schema, FAQ, TL;DR)
bhi waisa hi chalta rahega.

## Naya Setup (in cheezon ke liye)

### 1. Search Console mein site verify karein (agar pehle se nahi hai)
1. https://search.google.com/search-console/ pe jayein
2. "documentconvertfree.blogspot.com" add/verify karein (Blogger sites
   usually HTML tag ya DNS se verify hoti hain — dashboard instructions
   follow karein)

### 2. Google Cloud pe Search Console API enable karein
1. https://console.cloud.google.com/apis/library/searchconsole.googleapis.com
2. Apna existing project select karein → **Enable**

### 3. Naya token generate karein (purana kaam nahi karega — naya scope chahiye)
```
python get_token.py
```
Browser khulega, login karein, is dafa **2 permissions** manegi (Blogger +
Search Console) — dono allow karein. Naya JSON print hoga.

### 4. GitHub Secrets update/add karein
| Secret name | Value |
|---|---|
| `GOOGLE_TOKEN` | (update karein) naya JSON jo abhi generate hua |
| `SITE_URL` | `https://documentconvertfree.blogspot.com/` (Search Console mein jo property URL hai, bilkul wahi likhein — trailing slash sameet) |
| `PAGESPEED_API_KEY` | (optional lekin recommended) — free key yahan se banayein: https://console.cloud.google.com/apis/credentials → "Create Credentials" → "API key" → us key ko "PageSpeed Insights API" tak restrict kar dein |

### 5. (Optional) Features on/off karein
Workflow file (`.github/workflows/seo-agent.yml`) mein yeh env vars already
hain, agar koi feature band karni ho to secrets ki jagah seedha workflow
file mein `"false"` likh dein:
- `ENABLE_GSC` — Search Console data (default: true)
- `ENABLE_PAGESPEED` — page speed check (default: true)
- `ENABLE_BROKEN_LINKS` — broken link check (default: true)

## Test karein
Actions tab → "SEO GEO AEO Agent" → "Run workflow" → `dry_run = true` se
pehle test karein. Report (`reports/` folder) mein ab priority score, GSC
data, PageSpeed score, aur broken links bhi dikhengi.

## Ab bhi kya nahi karta (limitations)
- **Google Analytics** abhi shamil nahi hai (Search Console traffic/ranking
  data deta hai jo zyada directly SEO se juda hai; Analytics add karna
  agla step ho sakta hai agar chahiye)
- Content ko full rewrite nahi karta — sirf TL;DR + FAQ add karta hai,
  taake original content safe rahe
- Broken link checker sirf HTTP status check karta hai (404, 500 waghera),
  content ki quality ya redirect chains deep analyze nahi karta
- PageSpeed API free tier: bina key ke rate-limited hai; key se better hai

## Files
- `seo_agent.py` — main agent
- `audit_core.py` — on-page scoring engine
- `performance_data.py` — Search Console + PageSpeed + broken links (naya)
- `get_token.py` — OAuth token generator (ab dono scopes ke sath)
- `.github/workflows/seo-agent.yml` — weekly scheduled run
- `requirements.txt` — Python dependencies
