"""One-time credential bootstrap (LLD Figure 9). Run on your own computer, signed in to Azure.

`bootstrap-youtube` runs the OAuth authorization-code flow with PKCE on a loopback address, checks that the
token belongs to the expected channel, and writes the client id, client secret and refresh token straight to
Key Vault. Nothing is printed or saved to disk.
"""
from __future__ import annotations

import base64
import getpass
import hashlib
import http.server
import json
import secrets
import sys
import threading
import urllib.parse
import webbrowser

import httpx

from .common.config import Settings

SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly " \
         "https://www.googleapis.com/auth/youtube"


def _vault(settings: Settings):
    if not settings.key_vault_uri:
        raise SystemExit("set KEY_VAULT_URI (https://<vault>.vault.azure.net/) first")
    from azure.identity import AzureCliCredential
    from azure.keyvault.secrets import SecretClient

    return SecretClient(vault_url=settings.key_vault_uri, credential=AzureCliCredential())


def set_secret(name: str, settings: Settings) -> int:
    value = getpass.getpass(f"{name} (input hidden): ") if sys.stdin.isatty() else sys.stdin.read()
    value = value.strip()
    if not value:
        print("nothing entered; unchanged")
        return 1
    _vault(settings).set_secret(name, value)
    print(f"stored {name} in Key Vault")
    return 0


def bootstrap_youtube(client_secret_json: str, settings: Settings) -> int:
    with open(client_secret_json) as handle:
        raw = json.load(handle)
    conf = raw.get("installed") or raw.get("web") or {}
    client_id, client_secret = conf["client_id"], conf["client_secret"]
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Signed in. You can close this tab and return to the terminal.")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}/"
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": SCOPES,
        "access_type": "offline", "prompt": "consent", "state": state, "code_challenge": challenge,
        "code_challenge_method": "S256"})
    threading.Thread(target=server.handle_request, daemon=True).start()
    print(f"Sign in with the Google account that owns {settings.yt_channel_handle}:\n{url}\n")
    webbrowser.open(url)
    while "code" not in result and "error" not in result:
        threading.Event().wait(0.5)
    if result.get("state") != state or "code" not in result:
        raise SystemExit(f"sign-in failed: {result.get('error', 'state mismatch')}")
    token = httpx.post("https://oauth2.googleapis.com/token", data={
        "code": result["code"], "client_id": client_id, "client_secret": client_secret, "redirect_uri": redirect,
        "grant_type": "authorization_code", "code_verifier": verifier}, timeout=30).json()
    if "refresh_token" not in token:
        raise SystemExit(f"no refresh token returned: {token.get('error_description', token.get('error'))}")
    me = httpx.get("https://www.googleapis.com/youtube/v3/channels", params={"part": "snippet", "mine": "true"},
                   headers={"Authorization": f"Bearer {token['access_token']}"}, timeout=30).json()
    handle = ((me.get("items") or [{}])[0].get("snippet", {}).get("customUrl") or "").lower()
    if handle != settings.yt_channel_handle.lower():
        raise SystemExit(f"that account's channel is {handle!r}, not {settings.yt_channel_handle!r}; nothing stored")
    vault = _vault(settings)
    vault.set_secret("yt-client-id", client_id)
    vault.set_secret("yt-client-secret", client_secret)
    vault.set_secret("yt-refresh-token", token["refresh_token"])
    print(f"Stored YouTube credentials for {handle} in Key Vault.")
    return 0
