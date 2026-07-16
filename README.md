# Blogger SEO + GEO + AEO Agent

Yeh agent aapki Blogger site (documentconvertfree.blogspot.com) ke har post ko
har hafte automatically audit + fix karta hai. GitHub Actions pe free chalta
hai (public repo = unlimited free minutes).

## Kya karta hai

1. **Audit** — har post ka Technical SEO, Content Depth, GEO Readiness, Trust
   score (0-100).
2. **Auto-fix** (agar zaroorat ho):
   - Missing/lambi meta description theek karta hai
   - Missing labels/tags add karta hai
   - Images pe missing `alt` text add karta hai
   - FAQPage schema (JSON-LD) inject karta hai — AI answer engines (ChatGPT,
     AI Overviews) isko directly quote kar sakte hain
   - Post ke start mein ek "Quick answer" TL;DR paragraph add karta hai (AEO)
3. **Report** — `reports/` folder mein JSON + Markdown report save + commit
   hoti hai, taake history dekh sakein.

## Setup (ek dafa karna hai)

### 1. Naya GitHub repo banayein
Yeh poora folder GitHub pe push karein (public repo rakhein taake Actions
free ho — private repo bhi free hai kam usage ke liye).

### 2. Blogger Blog ID nikalein
Blogger dashboard -> Settings -> "Blog ID" wahan mil jayega. Ya URL se:
`https://www.blogger.com/blog/posts/<BLOG_ID>`

### 3. Google Cloud OAuth setup (aapke YouTube agent jaisa hi)
1. https://console.cloud.google.com/ -> apna existing project use karein
   (`tidal-vim-465701-d6`) ya naya banayein.
2. "APIs & Services" -> "Enable APIs" -> **Blogger API v3** enable karein.
3. "Credentials" -> "Create Credentials" -> "OAuth client ID" -> "Desktop app"
   -> download karein, naam `client_secret.json` rakh dein.

### 4. Apne PC pe token generate karein (ek dafa)
```
pip install google-auth-oauthlib google-api-python-client
python get_token.py
```
Browser khulega, apni Google account se login karein (jo Blogger site
manage karti hai), approve karein. Terminal mein ek JSON blob print hoga —
poora copy kar lein.

### 5. GitHub Secrets add karein
Repo -> Settings -> Secrets and variables -> Actions -> New repository secret:

| Secret name | Value |
|---|---|
| `BLOGGER_BLOG_ID` | Step 2 wala Blog ID |
| `GOOGLE_TOKEN` | Step 4 wala poora JSON blob |
| `GEMINI_API_KEY` | (optional) aapki free Gemini key — behtar auto-written meta descriptions/FAQs ke liye |

### 6. Test karein
GitHub repo -> "Actions" tab -> "SEO GEO AEO Agent" workflow -> "Run workflow"
-> pehle `dry_run = true` rakh ke test karein (koi changes nahi hongi, sirf
report banegi). Theek lage to `dry_run = false` karke dobara chalayein.

Iske baad yeh har Monday automatically chalta rahega.

## Files
- `seo_agent.py` — main agent (audit + auto-fix + report)
- `audit_core.py` — scoring engine (Technical/Content/GEO/Trust)
- `get_token.py` — one-time local OAuth helper
- `.github/workflows/seo-agent.yml` — weekly scheduled run
- `requirements.txt` — Python dependencies

## Limitations
- Yeh sirf on-page signals check karta hai (title, meta, headings, alt text,
  schema). Backlinks, Core Web Vitals/page speed, ya Search Console rankings
  check nahi karta.
- Content-level rewriting minimal rakha gaya hai (sirf TL;DR + FAQ add hota
  hai) taake aapka original content safe rahe — full paragraphs khud nahi
  badalta.
