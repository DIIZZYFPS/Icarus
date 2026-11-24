"""Spotify Web API search with the client-credentials flow (no user login, no Premium needed:
playback itself goes through the desktop app's MPRIS OpenUri)."""

import base64
import json
import time
import urllib.parse
import urllib.request
import asyncio


class SpotifyError(Exception):
    pass


class SpotifyClient:
    def __init__(self, client_id: str, client_secret: str, market: str = "US"):
        self.client_id, self.client_secret, self.market = client_id, client_secret, market
        self._token: str | None = None
        self._expires = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def _request(self, req: urllib.request.Request) -> dict:
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                return json.loads(resp.read())
        except Exception as e:
            raise SpotifyError(f"Spotify API error: {e}") from e

    def _get_token(self) -> str:
        if self._token and time.time() < self._expires - 30:
            return self._token
        auth = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        req = urllib.request.Request(
            "https://accounts.spotify.com/api/token",
            data=b"grant_type=client_credentials",
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
        )
        data = self._request(req)
        self._token, self._expires = data["access_token"], time.time() + int(data.get("expires_in", 3600))
        return self._token

    def _get(self, path: str, params: dict) -> dict:
        url = f"https://api.spotify.com/v1/{path}?{urllib.parse.urlencode(params)}"
        return self._request(urllib.request.Request(url, headers={"Authorization": f"Bearer {self._get_token()}"}))

    def _best_match(self, query: str, kind: str) -> dict | None:
        found = self._get("search", {"q": query, "type": kind, "limit": 1, "market": self.market})
        items = (found.get(f"{kind}s") or {}).get("items") or []
        item = next((i for i in items if i), None)
        if not item:
            return None
        if kind == "artist":
            # Opening an artist URI only shows the page; play their top track instead.
            top = self._get(f"artists/{item['id']}/top-tracks", {"market": self.market}).get("tracks") or []
            if top:
                return {"uri": top[0]["uri"], "label": f"{top[0]['name']} by {item['name']}"}
            return {"uri": item["uri"], "label": item["name"]}
        artists = ", ".join(a["name"] for a in item.get("artists", [])[:2])
        label = f"{item['name']} by {artists}" if artists else item["name"]
        if kind == "playlist":
            label = f"the playlist {item['name']}"
        return {"uri": item["uri"], "label": label}

    async def best_match(self, query: str, kind: str) -> dict | None:
        if not self.configured:
            raise SpotifyError("Spotify API keys aren't configured")
        return await asyncio.to_thread(self._best_match, query, kind)
