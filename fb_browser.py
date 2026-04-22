"""
Facebook browser automation using Playwright.
Opens a VISIBLE Chrome window — the user logs in manually, then this module
scrapes members/activity and automates member removal exactly as an admin
would do it by hand.
"""

import re
import time
import threading
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Set

from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout

FB = "https://www.facebook.com"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_uid(href: str) -> str:
    """Return a stable user identifier from a Facebook profile URL."""
    if not href:
        return ""
    href = href.split("#")[0].rstrip("/")
    # /groups/xxx/user/123456
    m = re.search(r"/user/(\d+)", href)
    if m:
        return m.group(1)
    # profile.php?id=123456
    m = re.search(r"[?&]id=(\d+)", href)
    if m:
        return m.group(1)
    # facebook.com/username  (strip query string first)
    clean = href.split("?")[0]
    m = re.search(r"facebook\.com/([^/]+)$", clean)
    if m:
        slug = m.group(1)
        _skip = {
            "groups", "pages", "watch", "marketplace", "gaming", "events",
            "memories", "saved", "friends", "login", "home", "notifications",
            "photo", "photos", "video", "videos", "stories", "reels",
        }
        if slug and slug not in _skip and not slug.startswith("pg"):
            return slug.lower()
    return ""


def _parse_relative_time(text: str) -> Optional[datetime]:
    """
    Convert Facebook relative timestamps ("3 hours ago", "2w", "Just now", …)
    into a datetime.  Returns None when the format is unrecognised.
    """
    if not text:
        return None
    now = datetime.now()
    t = text.lower().strip()
    if "just now" in t:
        return now

    patterns = [
        (r"(\d+)\s*s(?:ec|econd)?s?\b",  lambda n: now - timedelta(seconds=n)),
        (r"(\d+)\s*m(?:in|inute)?s?\b",  lambda n: now - timedelta(minutes=n)),
        (r"(\d+)\s*h(?:r|our)?s?\b",     lambda n: now - timedelta(hours=n)),
        (r"(\d+)\s*d(?:ay)?s?\b",        lambda n: now - timedelta(days=n)),
        (r"(\d+)\s*w(?:k|eek)?s?\b",     lambda n: now - timedelta(weeks=n)),
        (r"(\d+)\s*mo(?:nth)?s?\b",      lambda n: now - timedelta(days=n * 30)),
        (r"(\d+)\s*y(?:r|ear)?s?\b",     lambda n: now - timedelta(days=n * 365)),
    ]
    for pattern, calc in patterns:
        m = re.search(pattern, t)
        if m:
            return calc(int(m.group(1)))
    return None


def _group_id_from_input(raw: str) -> str:
    """Accept a full URL or bare ID/slug and return just the group identifier."""
    raw = raw.strip().rstrip("/")
    m = re.search(r"facebook\.com/groups/([^/?#]+)", raw)
    return m.group(1) if m else raw


# ── Browser controller ────────────────────────────────────────────────────────

class FBBrowser:
    def __init__(self):
        self._pw = None
        self._browser = None
        self._context = None
        self._page: Optional[Page] = None
        self.logged_in_as: str = ""

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def launch(self):
        """Open a visible Chromium window and go to Facebook login."""
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
            ],
        )
        self._context = self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            no_viewport=True,
        )
        self._page = self._context.new_page()
        self._page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        self._page.goto(f"{FB}/login", wait_until="domcontentloaded")

    def wait_for_login(
        self,
        timeout_s: int = 300,
        on_poll: Optional[Callable[[str], None]] = None,
    ) -> bool:
        """Block until the user finishes logging in (or timeout)."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                url = self._page.url
                if (
                    "facebook.com" in url
                    and "login" not in url
                    and "checkpoint" not in url
                ):
                    # Try to grab the user's name from the nav bar
                    try:
                        el = self._page.query_selector(
                            '[aria-label="Your profile"], '
                            'span[class*="profileName"]'
                        )
                        self.logged_in_as = el.inner_text().strip() if el else "Facebook User"
                    except Exception:
                        self.logged_in_as = "Facebook User"
                    return True
            except Exception:
                pass
            if on_poll:
                on_poll("Waiting for Facebook login…")
            time.sleep(1)
        return False

    def close(self):
        for obj in (self._page, self._context, self._browser, self._pw):
            try:
                if obj:
                    obj.close()
            except Exception:
                pass
        self._pw = self._browser = self._context = self._page = None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _dismiss(self):
        try:
            self._page.keyboard.press("Escape")
        except Exception:
            pass
        time.sleep(0.4)

    def _scroll_load(
        self,
        on_each: Callable[[], bool],
        scroll_px: int = 900,
        delay: float = 1.8,
        max_no_new: int = 5,
        stop_event: Optional[threading.Event] = None,
    ):
        """
        Scroll down the current page repeatedly, calling on_each() after each
        scroll.  Stops when on_each returns True or content stops loading.
        """
        no_new = 0
        prev_h = 0
        while not (stop_event and stop_event.is_set()):
            done = on_each()
            if done:
                break
            h = self._page.evaluate("document.body.scrollHeight")
            if h == prev_h:
                no_new += 1
                if no_new >= max_no_new:
                    break
            else:
                no_new = 0
            prev_h = h
            self._page.evaluate(f"window.scrollBy(0,{scroll_px})")
            time.sleep(delay)

    # ── Scrape members ────────────────────────────────────────────────────────

    def scrape_members(
        self,
        group_id: str,
        on_status: Optional[Callable[[str], None]] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> List[Dict]:
        def status(m):
            if on_status:
                on_status(m)

        status("Opening group members page…")
        self._page.goto(
            f"{FB}/groups/{group_id}/members",
            wait_until="domcontentloaded",
        )
        time.sleep(3)
        self._dismiss()

        seen: Set[str] = set()
        members: List[Dict] = []

        def collect() -> bool:
            for link in self._page.query_selector_all("a[href*='facebook.com/']"):
                try:
                    href = link.get_attribute("href") or ""
                    if not href or "/groups/" in href:
                        continue
                    uid = _extract_uid(href)
                    if not uid or uid in seen:
                        continue
                    seen.add(uid)

                    name = link.inner_text().strip()
                    if not name:
                        sp = link.query_selector("span")
                        name = sp.inner_text().strip() if sp else ""
                    if len(name) < 2:
                        continue

                    # Detect admin badge in the surrounding HTML
                    is_admin = False
                    try:
                        ctx_html = self._page.evaluate(
                            """el=>{let p=el;for(let i=0;i<7;i++){
                                if(!p.parentElement)break;p=p.parentElement;}
                                return p.innerHTML;}""",
                            link,
                        )
                        is_admin = bool(
                            re.search(r"\badmin\b|\bmoderator\b", ctx_html, re.I)
                        )
                    except Exception:
                        pass

                    members.append(
                        {
                            "id": uid,
                            "name": name,
                            "href": href.split("?")[0],
                            "admin": is_admin,
                            "active": False,
                        }
                    )
                except Exception:
                    continue

            status(f"Found {len(members)} members — keep scrolling…")
            return False  # never stop early; let scroll_load decide

        self._scroll_load(
            collect, scroll_px=800, delay=1.5, max_no_new=5, stop_event=stop_event
        )
        return members

    # ── Scrape activity ───────────────────────────────────────────────────────

    def scrape_active_users(
        self,
        group_id: str,
        months: int,
        on_status: Optional[Callable[[str], None]] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> Set[str]:
        def status(m):
            if on_status:
                on_status(m)

        cutoff = datetime.now() - timedelta(days=months * 30)
        active: Set[str] = set()
        past_cutoff = False
        articles_seen = 0

        status("Opening group feed…")
        self._page.goto(
            f"{FB}/groups/{group_id}",
            wait_until="domcontentloaded",
        )
        time.sleep(3)
        self._dismiss()

        def collect() -> bool:
            nonlocal past_cutoff, articles_seen
            articles = self._page.query_selector_all('[role="article"]')
            articles_seen = len(articles)

            for article in articles:
                try:
                    # ── Timestamp check ────────────────────────────────────
                    for ts_sel in ["abbr", "a[href*='?__cft__']", "span[id]"]:
                        for ts_el in article.query_selector_all(ts_sel):
                            raw = (
                                ts_el.get_attribute("aria-label")
                                or ts_el.get_attribute("title")
                                or ts_el.inner_text()
                                or ""
                            )
                            dt = _parse_relative_time(raw)
                            if dt and dt < cutoff:
                                past_cutoff = True
                                return True  # stop scrolling

                    # ── Collect all profile links in this post ─────────────
                    for link in article.query_selector_all("a[href*='facebook.com/']"):
                        href = link.get_attribute("href") or ""
                        if "/groups/" in href:
                            continue
                        uid = _extract_uid(href)
                        if uid:
                            active.add(uid)
                except Exception:
                    continue

            status(
                f"Scanned {articles_seen} posts — "
                f"{len(active)} active users found so far…"
            )
            return past_cutoff

        self._scroll_load(
            collect, scroll_px=1200, delay=2.0, max_no_new=5, stop_event=stop_event
        )
        return active

    # ── Remove a member ───────────────────────────────────────────────────────

    def remove_member(
        self,
        group_id: str,
        member: Dict,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> bool:
        def status(m):
            if on_status:
                on_status(m)

        name = member["name"]
        uid = member["id"]
        status(f"Removing {name}…")

        try:
            self._page.goto(
                f"{FB}/groups/{group_id}/members",
                wait_until="domcontentloaded",
            )
            time.sleep(2)
            self._dismiss()

            # Find this member's profile link on the page
            target = None
            for link in self._page.query_selector_all("a[href*='facebook.com/']"):
                try:
                    href = link.get_attribute("href") or ""
                    if _extract_uid(href) == uid:
                        target = link
                        break
                except Exception:
                    continue

            if not target:
                # Fall back: find by displayed name
                try:
                    target = self._page.get_by_text(name, exact=True).first
                except Exception:
                    pass

            if not target:
                status(f"Could not locate {name} on members page.")
                return False

            # Scroll the element into view and hover to reveal the options button
            target.scroll_into_view_if_needed()
            time.sleep(0.5)
            target.hover()
            time.sleep(0.8)

            # Look for a nearby popup/menu trigger
            menu_clicked = False
            for selector in [
                '[aria-haspopup="menu"]',
                '[aria-label*="More"]',
                '[aria-label*="more"]',
                'div[role="button"]',
            ]:
                try:
                    btns = self._page.query_selector_all(selector)
                    if btns:
                        btns[-1].click()
                        time.sleep(1)
                        menu_clicked = True
                        break
                except Exception:
                    continue

            if not menu_clicked:
                status(f"No options menu found for {name}.")
                return False

            # Click the remove option
            removed = False
            for label in [
                "Remove member",
                "Remove from group",
                "Remove from Group",
                "Remove",
            ]:
                try:
                    self._page.get_by_text(label, exact=True).first.click(timeout=2000)
                    time.sleep(1)
                    removed = True
                    break
                except PWTimeout:
                    continue

            if not removed:
                return False

            # Confirm if a dialog appears
            for label in ["Remove", "Confirm", "Yes"]:
                try:
                    self._page.locator(
                        f'[role="dialog"] >> text="{label}"'
                    ).first.click(timeout=2000)
                    time.sleep(1)
                    break
                except PWTimeout:
                    continue

            time.sleep(1.5)
            return True

        except Exception as exc:
            status(f"Error removing {name}: {exc}")
            return False
