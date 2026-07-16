#!/usr/bin/env python3
"""
Run this ONCE on your own PC (Windows 10) to generate the OAuth token that
GitHub Actions will use to edit your Blogger posts.

Steps before running:
  1. Go to https://console.cloud.google.com/ -> use your existing project
     (tidal-vim-465701-d6, same one your YouTube agent uses, or a new one).
  2. Enable "Blogger API v3" for that project.
  3. Credentials -> Create OAuth client ID -> Desktop app -> download JSON
     -> save it here as client_secret.json (same folder as this script).
  4. pip install google-auth-oauthlib google-api-python-client
  5. python get_token.py
  6. It opens a browser -> log in with the Google account that owns the
     Blogger site -> approve.
  7. It prints a JSON blob -> copy ALL of it -> paste as the value of the
     GitHub secret named GOOGLE_TOKEN (repo Settings -> Secrets and
     variables -> Actions -> New repository secret).
"""

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/blogger"]

flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
creds = flow.run_local_server(port=0)

print("\n\n===== COPY EVERYTHING BELOW INTO THE GitHub SECRET 'GOOGLE_TOKEN' =====\n")
print(creds.to_json())
print("\n===== END =====")
