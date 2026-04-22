"""
Activity-scanning logic.
Determines which group members were active within the last N months.
"""

import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Dict, Optional

from facebook_api import FacebookAPI, FacebookAPIError


class ActivityScanner:
    def __init__(self, api: FacebookAPI, group_id: str, months: int = 3):
        self.api = api
        self.group_id = group_id
        self.months = months
        cutoff = datetime.now(timezone.utc) - timedelta(days=months * 30)
        self.since_ts = int(cutoff.timestamp())

    def scan(
        self,
        on_progress: Optional[Callable[[float], None]] = None,
        on_status: Optional[Callable[[str], None]] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> Optional[List[Dict]]:
        """
        Scan the group for member activity.
        Returns a list of member dicts, or None if stopped.
        Each dict has: id, name, admin, active, last_seen.
        """

        def progress(pct: float):
            if on_progress:
                on_progress(pct)

        def status(msg: str):
            if on_status:
                on_status(msg)

        def stopped() -> bool:
            return bool(stop_event and stop_event.is_set())

        active_ids: set = set()

        # ── 1. Members ────────────────────────────────────────────────────────
        status("Fetching group members…")
        try:
            members = self.api.get_group_members(self.group_id, stop_event=stop_event)
        except FacebookAPIError as exc:
            raise
        if stopped():
            return None
        progress(15)
        status(f"Found {len(members)} members. Fetching recent posts…")

        # ── 2. Posts ──────────────────────────────────────────────────────────
        try:
            posts = self.api.get_group_feed(
                self.group_id, self.since_ts, stop_event=stop_event
            )
        except FacebookAPIError as exc:
            posts = []
            status(f"Warning: could not retrieve posts ({exc}). Continuing…")

        if stopped():
            return None
        progress(35)

        total = len(posts)
        status(f"Analysing {total} posts for comments and reactions…")

        # ── 3. Activity per post ──────────────────────────────────────────────
        for i, post in enumerate(posts):
            if stopped():
                return None

            uid = post.get("from", {}).get("id")
            if uid:
                active_ids.add(uid)

            post_id = post.get("id", "")
            if post_id:
                active_ids.update(
                    self.api.get_post_comments(post_id, stop_event=stop_event)
                )
                active_ids.update(
                    self.api.get_post_reactions(post_id, stop_event=stop_event)
                )

            progress(35 + 60 * (i + 1) / max(total, 1))
            if (i + 1) % 10 == 0 or (i + 1) == total:
                status(f"Analysed {i + 1}/{total} posts…")

        # ── 4. Compile results ────────────────────────────────────────────────
        progress(97)
        status("Compiling results…")

        results = []
        for m in members:
            is_active = m["id"] in active_ids
            results.append(
                {
                    "id": m["id"],
                    "name": m.get("name", "Unknown"),
                    "admin": bool(m.get("administrator", False)),
                    "active": is_active,
                }
            )

        # Sort: inactive first, then alphabetical
        results.sort(key=lambda r: (r["active"], r["name"].lower()))

        progress(100)
        return results
