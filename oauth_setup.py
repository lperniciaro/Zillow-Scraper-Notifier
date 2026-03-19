"""
oauth_setup.py
==============
One-time OAuth 2.0 setup helper.  Run this ONCE on your host machine
(not inside Docker) to generate token.json.

Prerequisites:
  pip install google-auth-oauthlib

Steps:
  1. Download your OAuth credentials from Google Cloud Console:
       https://console.cloud.google.com/
       → APIs & Services → Credentials → OAuth 2.0 Client IDs
       → Download JSON → save as  credentials.json  in this folder.

  2. Run this script:
       python oauth_setup.py

  3. A browser window will open.  Log in with the Gmail account you want
     to send alerts FROM and click "Allow".

  4. token.json is written to this folder.  Both files are automatically
     volume-mounted into the Docker container by docker-compose.yml.

  5. The container will auto-refresh the access token as needed using the
     refresh token stored in token.json — you should never need to re-run
     this unless you revoke access or delete token.json.
"""

import sys
from pathlib import Path

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    print(
        "ERROR: google-auth-oauthlib is not installed.\n"
        "Run:  pip install google-auth-oauthlib"
    )
    sys.exit(1)

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

CREDENTIALS_PATH = Path("credentials.json")
TOKEN_PATH       = Path("token.json")


def main() -> None:
    if not CREDENTIALS_PATH.exists():
        print(
            f"ERROR: {CREDENTIALS_PATH} not found.\n\n"
            "Download it from Google Cloud Console:\n"
            "  https://console.cloud.google.com/\n"
            "  → APIs & Services → Credentials\n"
            "  → OAuth 2.0 Client IDs → Download JSON\n"
            "  → save as credentials.json in this folder.\n"
        )
        sys.exit(1)

    print("Starting OAuth 2.0 flow...")
    print("A browser window will open. Log in and click Allow.\n")

    flow = InstalledAppFlow.from_client_secrets_file(
        str(CREDENTIALS_PATH),
        scopes=SCOPES,
    )
    # run_local_server opens a browser and handles the redirect automatically
    creds = flow.run_local_server(port=0, open_browser=True)

    TOKEN_PATH.write_text(creds.to_json())
    print(f"\n✅  token.json written to: {TOKEN_PATH.resolve()}")
    print("You can now start the Docker stack:  docker compose up -d --build")


if __name__ == "__main__":
    main()
