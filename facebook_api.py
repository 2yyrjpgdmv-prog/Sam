"""
Facebook Graph API wrapper.
Handles pagination, retries, and error normalisation.
"""

import time
import requests

GRAPH_VERSION = "v19.0"
BASE_URL = f"https://graph.facebook.com/{GRAPH_VERSION}"


class FacebookAPIError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class FacebookAPI:
    def __init__(self, access_token: str):
        self.access_token = access_token
        self._session = requests.Session()
        self._session.params = {"access_token": access_token}

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _request(self, method: str, path: str, **kwargs):
        url = f"{BASE_URL}/{path.lstrip('/')}"
        for attempt in range(3):
            try:
                resp = self._session.request(method, url, timeout=30, **kwargs)
                data = resp.json()
                if "error" in data:
                    err = data["error"]
                    raise FacebookAPIError(
                        err.get("message", "Unknown API error"),
                        err.get("code"),
                    )
                resp.raise_for_status()
                return data
            except FacebookAPIError:
                raise
            except requests.RequestException as exc:
                if attempt == 2:
                    raise FacebookAPIError(str(exc))
                time.sleep(2 ** attempt)

    def _get(self, path: str, params: dict = None):
        return self._request("GET", path, params=params or {})

    def _delete(self, path: str, params: dict = None):
        return self._request("DELETE", path, params=params or {})

    def _paginate(self, path: str, params: dict, stop_event=None) -> list:
        """Yield all items across cursor-paginated responses."""
        items = []
        while True:
            if stop_event and stop_event.is_set():
                break
            data = self._get(path, params)
            batch = data.get("data", [])
            items.extend(batch)
            after = data.get("paging", {}).get("cursors", {}).get("after")
            if not after or not batch:
                break
            params = {**params, "after": after}
            time.sleep(0.15)
        return items

    # ── Public API ────────────────────────────────────────────────────────────

    def get_me(self) -> dict:
        return self._get("me", {"fields": "id,name"})

    def get_group_info(self, group_id: str) -> dict:
        return self._get(group_id, {"fields": "id,name,member_count"})

    def get_group_members(self, group_id: str, stop_event=None) -> list:
        return self._paginate(
            f"{group_id}/members",
            {"fields": "id,name,administrator", "limit": 200},
            stop_event=stop_event,
        )

    def get_group_feed(self, group_id: str, since_ts: int, stop_event=None) -> list:
        return self._paginate(
            f"{group_id}/feed",
            {"fields": "id,from,created_time", "since": since_ts, "limit": 100},
            stop_event=stop_event,
        )

    def get_post_comments(self, post_id: str, stop_event=None) -> set:
        """Return set of user IDs who commented on this post."""
        try:
            items = self._paginate(
                f"{post_id}/comments",
                {"fields": "from", "limit": 200},
                stop_event=stop_event,
            )
        except FacebookAPIError:
            return set()
        return {c["from"]["id"] for c in items if c.get("from", {}).get("id")}

    def get_post_reactions(self, post_id: str, stop_event=None) -> set:
        """Return set of user IDs who reacted to this post."""
        try:
            items = self._paginate(
                f"{post_id}/reactions",
                {"fields": "id", "limit": 200},
                stop_event=stop_event,
            )
        except FacebookAPIError:
            return set()
        return {r["id"] for r in items if r.get("id")}

    def remove_member(self, group_id: str, user_id: str) -> dict:
        return self._delete(f"{group_id}/members/{user_id}")
