"""
Facebook browser automation using Playwright.
Opens a VISIBLE Chrome window — the user logs in manually, then this module
scrapes members/activity and automates member removal exactly as an admin
would do it by hand.
"""

import os
import re
import time
import random
import threading
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Set

from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout

FB = "https://www.facebook.com"
SESSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session.json")


# ── Stealth helpers ───────────────────────────────────────────────────────────

def _delay(lo: float = 0.8, hi: float = 2.2):
    """Sleep for a random duration to mimic human reaction time."""
    time.sleep(random.uniform(lo, hi))


def _scroll(page: Page, base_px: int = 900):
    """Scroll by a randomised amount — humans don't scroll in exact increments."""
    amount = base_px + random.randint(-250, 350)
    # Occasionally do a tiny scroll-back before continuing (human habit)
    if random.random() < 0.12:
        page.evaluate(f"window.scrollBy(0, -{random.randint(30, 120)})")
        time.sleep(random.uniform(0.15, 0.4))
    page.evaluate(f"window.scrollBy(0, {amount})")


def _mouse_to(page: Page, element):
    """
    Move the mouse to an element with slight positional jitter and a
    micro-pause mid-travel so the trajectory looks human.
    """
    try:
        box = element.bounding_box()
        if not box:
            return
        tx = box["x"] + box["width"]  * random.uniform(0.25, 0.75)
        ty = box["y"] + box["height"] * random.uniform(0.25, 0.75)
        # Move to a nearby point first
        page.mouse.move(
            tx + random.randint(-18, 18),
            ty + random.randint(-18, 18),
        )
        time.sleep(random.uniform(0.05, 0.18))
        page.mouse.move(tx, ty)
        time.sleep(random.uniform(0.05, 0.12))
    except Exception:
        pass


def _click(page: Page, element):
    """Human-like click: move mouse to element, brief hover, then click."""
    _mouse_to(page, element)
    element.click()


# ── URL helpers ───────────────────────────────────────────────────────────────

def _extract_uid(href: str) -> str:
    """Return a stable user identifier from a Facebook profile URL
    (absolute or relative)."""
    if not href:
        return ""
    href = href.split("#")[0].rstrip("/")
    # /user/<digits>
    m = re.search(r"/user/(\d+)", href)
    if m:
        return m.group(1)
    # profile.php?id=<digits> or ?...&id=<digits>
    m = re.search(r"[?&]id=(\d+)", href)
    if m:
        return m.group(1)
    # Strip query
    clean = href.split("?")[0]
    _skip = {
        "groups", "pages", "watch", "marketplace", "gaming", "events",
        "memories", "saved", "friends", "login", "home", "notifications",
        "photo", "photos", "video", "videos", "stories", "reels",
        "help", "settings", "privacy", "ads", "business", "messages",
        "hashtag", "share", "sharer", "dialog", "l.php", "lm.facebook.com",
        "profile.php",
    }
    # Absolute: facebook.com/<slug>
    m = re.search(r"facebook\.com/([^/]+)$", clean)
    if m:
        slug = m.group(1)
        if slug and slug not in _skip and not slug.startswith("pg"):
            return slug.lower()
    # Relative: /<slug>  (no additional path segments)
    m = re.fullmatch(r"/([^/]+)", clean)
    if m:
        slug = m.group(1)
        if slug and slug not in _skip and not slug.startswith("pg"):
            return slug.lower()
    return ""


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_relative_time(text: str) -> Optional[datetime]:
    """Convert Facebook relative or absolute timestamps into a datetime."""
    if not text:
        return None
    now = datetime.now()
    t = text.lower().strip()
    if "just now" in t or "now" == t:
        return now
    if "today" in t:
        return now
    if "yesterday" in t:
        return now - timedelta(days=1)

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

    # "April 22 at 2:15 PM" / "April 22, 2024" / "22 April" / "January 28"
    m = re.search(
        r"\b("
        r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
        r")[a-z]*\s+(\d{1,2})(?:[,\s]+(\d{4}))?",
        t,
    )
    if not m:
        m = re.search(
            r"\b(\d{1,2})\s+("
            r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
            r")[a-z]*(?:[,\s]+(\d{4}))?",
            t,
        )
        if m:
            day  = int(m.group(1))
            mon  = _MONTHS[m.group(2)]
            year = int(m.group(3)) if m.group(3) else now.year
        else:
            return None
    else:
        mon  = _MONTHS[m.group(1)]
        day  = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else now.year

    try:
        dt = datetime(year, mon, day)
    except ValueError:
        return None
    # Facebook omits the year for recent dates — if the result is in the
    # future by more than a day, it was actually last year.
    if dt > now + timedelta(days=1):
        try:
            dt = datetime(year - 1, mon, day)
        except ValueError:
            return None
    return dt


def _parse_joined(text: str):
    """Extract join-age from member-card text. Returns (days, display_text)."""
    if not text:
        return None, ""
    t = text.lower()
    patterns = [
        (r"joined\s+(\d+)\s*day",      1,   "day"),
        (r"joined\s+(\d+)\s*week",     7,   "week"),
        (r"joined\s+(\d+)\s*month",    30,  "month"),
        (r"joined\s+(\d+)\s*year",     365, "year"),
        (r"member for\s+(\d+)\s*day",  1,   "day"),
        (r"member for\s+(\d+)\s*week", 7,   "week"),
        (r"member for\s+(\d+)\s*month",30,  "month"),
        (r"member for\s+(\d+)\s*year", 365, "year"),
    ]
    for pattern, mult, unit in patterns:
        m = re.search(pattern, t)
        if m:
            n = int(m.group(1))
            days = n * mult
            label = f"{n} {unit}{'s' if n != 1 else ''} ago"
            return days, label
    return None, ""


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

    def launch(self) -> bool:
        """
        Open a visible Chromium window.
        Returns True if the saved session is still valid (already logged in),
        False if the user needs to log in.
        """
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
            ],
        )

        ctx_kwargs = dict(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            no_viewport=True,
        )
        if os.path.exists(SESSION_FILE):
            ctx_kwargs["storage_state"] = SESSION_FILE

        self._context = self._browser.new_context(**ctx_kwargs)
        self._page = self._context.new_page()
        self._page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )

        if os.path.exists(SESSION_FILE):
            self._page.goto(f"{FB}/", wait_until="domcontentloaded")
            time.sleep(2)
            # Check immediately if session is still valid
            url = self._page.url
            _login_paths = ("/login", "/reg", "/recover", "login.php")
            if "facebook.com" in url and not any(p in url for p in _login_paths):
                self._grab_username()
                self._save_session()
                return True  # already logged in — no action needed
            # Session expired — go to login page
            self._page.goto(f"{FB}/login", wait_until="domcontentloaded")
        else:
            self._page.goto(f"{FB}/login", wait_until="domcontentloaded")

        return False  # needs login

    def wait_for_login(
        self,
        timeout_s: int = 300,
        on_poll: Optional[Callable[[str], None]] = None,
        confirm_event: Optional[threading.Event] = None,
    ) -> bool:
        """
        Wait until Facebook login is detected automatically, OR until the
        caller signals confirm_event (user clicked "I'm Logged In").
        """
        # Login-page URL fragments — if ANY of these appear we're not logged in
        _login_paths = ("/login", "/reg", "/recover", "login.php")

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            # Manual confirmation from the UI button
            if confirm_event and confirm_event.is_set():
                self._grab_username()
                self._save_session()
                return True

            try:
                url = self._page.url
                on_login_page = any(p in url for p in _login_paths)
                if "facebook.com" in url and not on_login_page:
                    self._grab_username()
                    self._save_session()
                    return True
            except Exception:
                pass

            if on_poll:
                on_poll(
                    "Waiting for login… "
                    "(log in to Facebook in the browser, then click \"I'm Logged In\")"
                )
            time.sleep(1)
        return False

    def _grab_username(self):
        try:
            el = self._page.query_selector(
                '[aria-label="Your profile"], span[class*="profileName"]'
            )
            self.logged_in_as = el.inner_text().strip() if el else "Facebook User"
        except Exception:
            self.logged_in_as = "Facebook User"

    def _save_session(self):
        try:
            self._context.storage_state(path=SESSION_FILE)
        except Exception:
            pass

    def clear_session(self):
        """Delete the saved session file (forces a fresh login next time)."""
        try:
            if os.path.exists(SESSION_FILE):
                os.remove(SESSION_FILE)
        except Exception:
            pass

    def close(self):
        # Save session before closing so it persists for next run
        self._save_session()
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
        time.sleep(random.uniform(0.3, 0.6))

    # ── Comment / reactor expansion ───────────────────────────────────────────

    _COMMENT_BUTTON_PATTERNS = (
        "view previous comment",
        "view more comment",
        "view all comment",
        "more comment",
        "previous comment",
    )

    # Text-like patterns that look like a Facebook timestamp (must be short
    # AND match a time shape — stops author-name strings from being parsed).
    _TIME_TEXT_RE = re.compile(
        r"("
        r"^\s*\d+\s*[smhdwy]\b"                      # 23h, 1d, 2w, 5min
        r"|\bjust\s*now\b"
        r"|\bnow\b"
        r"|\byesterday\b"
        r"|\btoday\b"
        r"|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}\b"
        r"|\b\d{1,2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b"
        r"|\b\d{1,2}:\d{2}\b"
        r")",
        re.IGNORECASE,
    )

    def _extract_post_timestamp(self, article):
        """Return (oldest_datetime, debug_list_of_candidate_texts) for the
        POST's own timestamp — never a comment's or the author's name."""
        candidates = []
        seen_texts: List[str] = []

        def _consider(raw: str):
            if not raw:
                return
            snippet = raw[:60].replace("\n", " ")
            if not self._TIME_TEXT_RE.search(snippet):
                return  # Not time-shaped — skip (author names never match).
            seen_texts.append(snippet[:40])
            dt = _parse_relative_time(snippet)
            if dt:
                candidates.append(dt)

        # Highest-confidence sources: post-permalink links (NOT comment perms).
        for sel in [
            'a[href*="/posts/"]',
            'a[href*="/permalink/"]',
            'a[href*="story_fbid"]',
        ]:
            for el in article.query_selector_all(sel):
                try:
                    href = el.get_attribute("href") or ""
                    if "comment_id" in href:
                        continue
                    # Skip if nested inside a comment card.
                    is_nested = self._page.evaluate(
                        """(el, art) => {
                            let p = el.parentElement;
                            while (p && p !== art) {
                                if (p.getAttribute &&
                                    p.getAttribute('role') === 'article') {
                                    return true;
                                }
                                p = p.parentElement;
                            }
                            return false;
                        }""",
                        el, article,
                    )
                    if is_nested:
                        continue
                    raw = (el.get_attribute("aria-label")
                           or el.get_attribute("title")
                           or el.inner_text()
                           or "")
                    _consider(raw)
                except Exception:
                    continue

        # Secondary: <abbr> (older Facebook renderings).
        for el in article.query_selector_all("abbr"):
            try:
                raw = (el.get_attribute("title")
                       or el.get_attribute("aria-label")
                       or el.inner_text()
                       or "")
                _consider(raw)
            except Exception:
                continue

        return (min(candidates) if candidates else None, seen_texts)

    def _expand_comments(self, article, stop_event=None, max_clicks: int = 6):
        """Click 'View N more comments' / 'View replies' buttons inside an
        article so the hidden commenters become visible to the link scraper."""
        for _ in range(max_clicks):
            if stop_event and stop_event.is_set():
                return
            clicked = False
            try:
                buttons = article.query_selector_all('div[role="button"]')
            except Exception:
                return
            for btn in buttons:
                try:
                    txt = (btn.inner_text() or "").strip().lower()
                except Exception:
                    continue
                if not txt or len(txt) > 60:
                    continue
                if not any(p in txt for p in self._COMMENT_BUTTON_PATTERNS):
                    continue
                try:
                    btn.scroll_into_view_if_needed(timeout=800)
                    btn.click(timeout=1200)
                    clicked = True
                    time.sleep(random.uniform(0.5, 1.0))
                    break  # re-query after each click — DOM changes
                except Exception:
                    continue
            if not clicked:
                return

    _REACTOR_BUTTON_PATTERNS = (
        "see who reacted",
        "people reacted",
        "person reacted",
        "reactions",
        "all reactions",
    )

    def _scrape_reactors(self, article, active: Set[str], stop_event=None):
        """Click the POST'S reactor count (not any nested comment's), scrape
        names from the modal, then close it. Returns (label, names_captured)
        where label is the aria-label/text of the clicked button (for debug),
        and names_captured is how many UIDs were added from the modal."""
        if stop_event and stop_event.is_set():
            return ("", 0)
        pre_count = len(active)
        label = ""
        try:
            # Find a button that belongs to THIS post (not nested in a comment)
            # and whose ARIA-LABEL clearly identifies it as the reactor count.
            # We DO NOT fall back to plain text matching — that was picking up
            # timestamps ("22m") and reply counts ("1", "3") by mistake.
            handle = self._page.evaluate_handle(
                """(article) => {
                    // Strong aria-label signals that mean "the reactor count".
                    const strong = [
                        'all reactions',
                        'see who reacted',
                        'see all reactions',
                    ];
                    // Regex signals for aria-labels like
                    // "42 likes", "3 loves", "Tyson, Eric and 40 others".
                    const countRE = /^\\s*\\d+\\s+(likes?|loves?|laughs?|hahas?|wows?|sads?|angrys?|cares?|reactions?|others?)\\b/i;
                    const summaryRE = /^[\\w .'\\-]{2,40},\\s+[\\w .'\\-]{2,40}(,[^,]+){0,2}\\s+and\\s+\\d+\\s+others?/i;

                    const all = article.querySelectorAll('[aria-label]');
                    for (const c of all) {
                        // Skip if nested inside another role=article (a comment).
                        let p = c.parentElement;
                        let nested = false;
                        while (p && p !== article) {
                            if (p.getAttribute &&
                                p.getAttribute('role') === 'article') {
                                nested = true;
                                break;
                            }
                            p = p.parentElement;
                        }
                        if (nested) continue;

                        const al = (c.getAttribute('aria-label') || '').toLowerCase();
                        if (!al || al.length > 200) continue;

                        if (strong.some(k => al.includes(k))) {
                            c.__hit_label = 'aria: ' + al.slice(0,80);
                            return c;
                        }
                        if (countRE.test(al)) {
                            c.__hit_label = 'aria-count: ' + al.slice(0,80);
                            return c;
                        }
                        if (summaryRE.test(al)) {
                            c.__hit_label = 'aria-summary: ' + al.slice(0,80);
                            return c;
                        }
                    }
                    return null;
                }""",
                article,
            )
            target = handle.as_element() if handle else None
            if target is None:
                return ("", 0)
            try:
                label = self._page.evaluate(
                    "(el)=>el.__hit_label || (el.getAttribute && el.getAttribute('aria-label')) || (el.innerText||'').slice(0,40)",
                    target,
                ) or ""
            except Exception:
                label = ""
            target.scroll_into_view_if_needed(timeout=800)
            # Human pre-click pause + mouse hover for realism.
            time.sleep(random.uniform(2.0, 3.6))
            try:
                _mouse_to(self._page, target)
            except Exception:
                pass
            time.sleep(random.uniform(0.4, 0.9))
            target.click(timeout=1500)
        except Exception:
            return (label, 0)

        # Wait for modal to render — Facebook often lazy-loads the list.
        time.sleep(random.uniform(2.0, 3.2))
        try:
            dialog = self._page.query_selector('[role="dialog"]')
            if dialog is None:
                self._dismiss()
                return (label, 0)

            # Scroll the dialog slowly — one scroll every 1.5–3 seconds, 6
            # scrolls total. Mimics a human browsing the reactor list.
            for i in range(6):
                if stop_event and stop_event.is_set():
                    break
                try:
                    self._page.evaluate(
                        "(d)=>{const ss=d.querySelectorAll('*');"
                        "for(const e of ss){if(e.scrollHeight>e.clientHeight+50)"
                        "{e.scrollTop+=Math.max(200, e.clientHeight*0.7);"
                        " break;}}}",
                        dialog,
                    )
                except Exception:
                    pass
                time.sleep(random.uniform(1.5, 3.0))

            # Quick pause to "read" before scraping.
            time.sleep(random.uniform(0.6, 1.2))

            for link in dialog.query_selector_all("a[href]"):
                try:
                    href = link.get_attribute("href") or ""
                    uid = _extract_uid(href)
                    if uid:
                        active.add(uid)
                except Exception:
                    continue
        finally:
            self._dismiss()
            # Post-close breathing room before we touch the next post.
            time.sleep(random.uniform(1.2, 2.2))
        return (label, len(active) - pre_count)

    def _scroll_load(
        self,
        on_each: Callable[[], bool],
        scroll_px: int = 900,
        delay_lo: float = 1.4,
        delay_hi: float = 2.6,
        max_no_new: int = 5,
        stop_event: Optional[threading.Event] = None,
    ):
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
            _scroll(self._page, scroll_px)
            _delay(delay_lo, delay_hi)

    # ── Scrape members ────────────────────────────────────────────────────────

    def scrape_members(
        self,
        group_id: str,
        active_set: Optional[Set[str]] = None,
        inactive_limit: int = 0,
        on_status: Optional[Callable[[str], None]] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> List[Dict]:
        def status(m):
            if on_status:
                on_status(m)

        active_set = active_set or set()

        status("Opening group members page…")
        self._page.goto(
            f"{FB}/groups/{group_id}/members",
            wait_until="domcontentloaded",
        )
        _delay(2.5, 4.0)
        self._dismiss()

        seen: Set[str] = set()
        members: List[Dict] = []
        inactive_count = 0

        def collect() -> bool:
            nonlocal inactive_count
            for link in self._page.query_selector_all("a[href]"):
                try:
                    href = link.get_attribute("href") or ""
                    if not href:
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

                    is_admin = False
                    ctx_html = ""
                    ctx_text = ""
                    try:
                        ctx_html = self._page.evaluate(
                            """el=>{let p=el;for(let i=0;i<7;i++){
                                if(!p.parentElement)break;p=p.parentElement;}
                                return p.innerHTML;}""",
                            link,
                        )
                        ctx_text = self._page.evaluate(
                            """el=>{let p=el;for(let i=0;i<7;i++){
                                if(!p.parentElement)break;p=p.parentElement;}
                                return p.innerText;}""",
                            link,
                        )
                        is_admin = bool(
                            re.search(r"\badmin\b|\bmoderator\b", ctx_html, re.I)
                        )
                    except Exception:
                        pass

                    joined_days, joined_text = _parse_joined(ctx_text)
                    new_member = joined_days is not None and joined_days < 30
                    is_active = (uid in active_set) or new_member

                    members.append({
                        "id": uid,
                        "name": name,
                        "href": href.split("?")[0],
                        "admin": is_admin,
                        "active": is_active,
                        "new_member": new_member,
                        "joined_days": joined_days,
                        "joined_text": joined_text,
                    })

                    if not is_active and not is_admin:
                        inactive_count += 1
                except Exception:
                    continue

            status(
                f"Found {len(members)} members — "
                f"{inactive_count} inactive so far…"
            )
            if inactive_limit > 0 and inactive_count >= inactive_limit:
                return True
            return False

        self._scroll_load(
            collect,
            scroll_px=800,
            delay_lo=1.3,
            delay_hi=2.4,
            max_no_new=5,
            stop_event=stop_event,
        )
        return members

    # ── Scrape activity ───────────────────────────────────────────────────────

    def scrape_active_users(
        self,
        group_id: str,
        days: int,
        on_status: Optional[Callable[[str], None]] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> Set[str]:
        def status(m):
            if on_status:
                on_status(m)

        cutoff = datetime.now() - timedelta(days=days)
        active: Set[str] = set()
        articles_seen = 0
        consecutive_old = 0
        CUTOFF_STREAK = 2  # need 2 old-in-a-row before stopping
        seen_article_ids = set()
        debug_lines: List[str] = [
            f"Group: {group_id}",
            f"Days cutoff: {days} (cutoff = {cutoff})",
            "",
        ]

        status("Opening group feed…")
        self._page.goto(
            f"{FB}/groups/{group_id}",
            wait_until="domcontentloaded",
        )
        _delay(2.5, 4.0)
        self._dismiss()

        # Wait for real posts to render — Facebook shows skeleton "Loading..."
        # placeholders first; those aren't usable.
        status("Waiting for feed content to load…")
        got_real = self._wait_for_feed(timeout_s=25.0)
        if not got_real:
            status("Feed content did not load within 25s — dumping anyway.")

        # Raw diagnostic dump: write the first few role="article" elements'
        # outerHTML so we can see the actual DOM shape Facebook is using today.
        self._dump_raw_articles(group_id)

        def collect() -> bool:
            nonlocal articles_seen, consecutive_old
            # Filter to REAL POSTS only. A post article has at least one
            # /posts/ or /permalink/ link WITHOUT comment_id=...  — comment
            # cards only have permalink hrefs that include comment_id.
            all_articles = self._page.query_selector_all('[role="article"]')
            articles = []
            for art in all_articles:
                try:
                    is_real_post = self._page.evaluate(
                        """(el) => {
                            // Skip if nested inside another article (defensive).
                            let p = el.parentElement;
                            while (p) {
                                if (p.getAttribute &&
                                    p.getAttribute('role') === 'article') {
                                    return false;
                                }
                                p = p.parentElement;
                            }
                            // Must have a post-permalink link that is NOT a
                            // comment permalink.
                            const links = el.querySelectorAll(
                                'a[href*="/posts/"], a[href*="/permalink/"],'
                                + ' a[href*="story_fbid"]'
                            );
                            for (const a of links) {
                                const href = a.getAttribute('href') || '';
                                if (!href.includes('comment_id')) {
                                    return true;
                                }
                            }
                            return false;
                        }""",
                        art,
                    )
                except Exception:
                    is_real_post = False
                if is_real_post:
                    articles.append(art)
            articles_seen = len(articles)

            for article in articles:
                if stop_event and stop_event.is_set():
                    return True
                # Skip articles we already processed to avoid double-work.
                try:
                    art_key = self._page.evaluate(
                        """(el) => {
                            const hrefs = Array.from(
                                el.querySelectorAll('a[href]')
                            ).slice(0,3).map(a=>a.getAttribute('href')||'').join('|');
                            return (el.innerText||'').slice(0,120) + '##' + hrefs;
                        }""",
                        article,
                    )
                except Exception:
                    art_key = None
                if art_key and art_key in seen_article_ids:
                    continue
                if art_key:
                    seen_article_ids.add(art_key)

                try:
                    # Oldest timestamp in article = post's own date.
                    # Use TARGETED selectors that only hit post-permalink links
                    # (never author or comment permalinks).
                    oldest_dt, all_timestamps = self._extract_post_timestamp(
                        article,
                    )

                    is_old = bool(oldest_dt and oldest_dt < cutoff)
                    if is_old:
                        consecutive_old += 1
                    else:
                        consecutive_old = 0

                    pre_count = len(active)

                    # Only do the heavy click-work if the post is within the
                    # cutoff window — no point opening modals for old posts.
                    react_label = ""
                    reactors = 0
                    if not is_old:
                        # Comment expansion temporarily disabled — it was
                        # polluting the article list by revealing comment
                        # cards as sibling role="article" elements.
                        # self._expand_comments(article, stop_event)
                        react_label, reactors = self._scrape_reactors(
                            article, active, stop_event,
                        )

                        for link in article.query_selector_all("a[href]"):
                            href = link.get_attribute("href") or ""
                            uid = _extract_uid(href)
                            if uid:
                                active.add(uid)

                    gained = len(active) - pre_count
                    debug_lines.append(
                        f"Post #{len(seen_article_ids)}: "
                        f"oldest_ts={oldest_dt}  is_old={is_old}  "
                        f"streak={consecutive_old}  "
                        f"reactor_btn={react_label!r}  "
                        f"reactors_modal={reactors}  users_gained={gained}"
                    )
                    if len(all_timestamps) > 0:
                        debug_lines.append(
                            f"    timestamps seen: {all_timestamps[:8]}"
                        )

                    if consecutive_old >= CUTOFF_STREAK:
                        debug_lines.append(
                            f"Cutoff reached: {consecutive_old} old posts in a row"
                        )
                        return True
                except Exception as exc:
                    debug_lines.append(f"Post error: {exc}")
                    continue

            status(
                f"Scanned {len(seen_article_ids)} posts — "
                f"{len(active)} active users found so far…"
            )
            return False

        self._scroll_load(
            collect,
            scroll_px=1100,
            delay_lo=1.8,
            delay_hi=3.0,
            max_no_new=5,
            stop_event=stop_event,
        )

        # Always write a debug log so we can diagnose what happened per post.
        try:
            path = os.path.join(os.path.dirname(SESSION_FILE), "debug_scan.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(debug_lines))
                fh.write(f"\n\nTotal posts scanned: {len(seen_article_ids)}\n")
                fh.write(f"Total active users captured: {len(active)}\n")
            status(
                f"Scan done — {len(active)} users. Debug log: {path}"
            )
        except Exception:
            pass

        return active

    def _wait_for_feed(self, timeout_s: float = 25.0) -> bool:
        """Wait until at least one real (non-loading) role=article is present.
        Returns True if real content loaded, False on timeout."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                count = self._page.evaluate(
                    """() => {
                        const arts = document.querySelectorAll('[role="article"]');
                        let real = 0;
                        for (const a of arts) {
                            const loading = a.querySelector(
                                '[aria-label="Loading..."][role="status"]'
                            );
                            const text = (a.innerText || '').trim();
                            if (!loading && text.length > 30) real++;
                        }
                        return real;
                    }"""
                )
                if count >= 1:
                    return True
            except Exception:
                pass
            time.sleep(0.8)
        return False

    def _dump_raw_articles(self, group_id: str):
        """Write raw HTML of the first few REAL (non-loading) role="article"
        elements so we can see Facebook's actual DOM shape for posts today."""
        path = os.path.join(os.path.dirname(SESSION_FILE), "raw_debug.txt")
        try:
            # Filter to articles that aren't skeleton loading placeholders.
            all_arts = self._page.query_selector_all('[role="article"]')
            real_arts = []
            for art in all_arts:
                try:
                    is_loading = self._page.evaluate(
                        """(el) => {
                            if (el.querySelector(
                                '[aria-label="Loading..."][role="status"]'
                            )) return true;
                            const t = (el.innerText || '').trim();
                            return t.length < 30;
                        }""",
                        art,
                    )
                except Exception:
                    is_loading = False
                if not is_loading:
                    real_arts.append(art)
                if len(real_arts) >= 3:
                    break
            articles = real_arts[:3]
            lines = [
                f"Group: {group_id}",
                f"URL: {self._page.url}",
                f"Total role=article found: "
                f"{len(self._page.query_selector_all('[role=article]'))}",
                f"Dumping first {len(articles)} articles verbatim.",
                "",
            ]
            for i, article in enumerate(articles):
                lines.append(f"\n========== ARTICLE {i + 1} ==========")
                try:
                    outer = self._page.evaluate(
                        "(el) => el.outerHTML.slice(0, 4000)",
                        article,
                    )
                    lines.append("--- outerHTML (first 4000 chars) ---")
                    lines.append(outer or "")
                except Exception as exc:
                    lines.append(f"outerHTML error: {exc}")

                try:
                    text = article.inner_text() or ""
                    lines.append("\n--- inner_text (first 400) ---")
                    lines.append(text[:400].replace("\n", " | "))
                except Exception:
                    pass

                try:
                    links = article.query_selector_all("a[href]")
                    lines.append(f"\n--- a[href] count: {len(links)} ---")
                    for link in links[:25]:
                        try:
                            href = link.get_attribute("href") or ""
                            txt  = (link.inner_text() or "")[:60].replace("\n", " ")
                            al   = link.get_attribute("aria-label") or ""
                            lines.append(
                                f"  href={href[:160]!r} text={txt!r} aria={al[:80]!r}"
                            )
                        except Exception:
                            continue
                except Exception as exc:
                    lines.append(f"a[href] error: {exc}")

                try:
                    ariad = article.query_selector_all("[aria-label]")
                    lines.append(f"\n--- [aria-label] count: {len(ariad)} ---")
                    for el in ariad[:30]:
                        try:
                            al = el.get_attribute("aria-label") or ""
                            tag = self._page.evaluate(
                                "(el)=>el.tagName+(el.getAttribute('role')?'['+el.getAttribute('role')+']':'')",
                                el,
                            )
                            lines.append(f"  {tag}: {al[:140]!r}")
                        except Exception:
                            continue
                except Exception:
                    pass

            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines))
        except Exception:
            pass

    def _dump_feed_debug(self, group_id: str, status: Callable[[str], None]):
        path = os.path.join(os.path.dirname(SESSION_FILE), "debug_scan.txt")
        try:
            articles = self._page.query_selector_all('[role="article"]')[:5]
            lines = [
                f"Group: {group_id}",
                f"URL: {self._page.url}",
                f"Articles captured: {len(articles)}",
                "",
            ]
            for i, article in enumerate(articles):
                lines.append(f"=== Article {i + 1} ===")
                try:
                    text = article.inner_text() or ""
                    lines.append(f"Text (first 300): {text[:300].replace(chr(10), ' | ')}")
                except Exception as exc:
                    lines.append(f"Text: <error {exc}>")

                try:
                    links = article.query_selector_all("a[href]")
                    lines.append(f"<a href> count: {len(links)}")
                    for link in links[:25]:
                        try:
                            href = link.get_attribute("href") or ""
                            txt  = (link.inner_text() or "")[:60].replace("\n", " ")
                            role = link.get_attribute("role") or ""
                            lines.append(f"  href={href!r}  role={role!r}  text={txt!r}")
                        except Exception:
                            continue
                except Exception as exc:
                    lines.append(f"<a href> query error: {exc}")

                lines.append("")

            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines))
            status(
                f"0 users found — wrote diagnostic to {path}. "
                f"Please share this file."
            )
        except Exception as exc:
            status(f"0 users found. Could not write debug file: {exc}")

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
        uid  = member["id"]
        status(f"Removing {name}…")

        try:
            self._page.goto(
                f"{FB}/groups/{group_id}/members",
                wait_until="domcontentloaded",
            )
            _delay(1.8, 3.2)
            self._dismiss()

            # Locate the member's link
            target = None
            for link in self._page.query_selector_all("a[href]"):
                try:
                    href = link.get_attribute("href") or ""
                    if _extract_uid(href) == uid:
                        target = link
                        break
                except Exception:
                    continue

            if not target:
                try:
                    target = self._page.get_by_text(name, exact=True).first
                except Exception:
                    pass

            if not target:
                status(f"Could not locate {name} on members page.")
                return False

            # Scroll member into view, hover with mouse movement
            target.scroll_into_view_if_needed()
            _delay(0.4, 0.9)
            _mouse_to(self._page, target)
            _delay(0.5, 1.0)

            # Find and click the nearby options/menu button
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
                        _mouse_to(self._page, btns[-1])
                        _delay(0.2, 0.5)
                        btns[-1].click()
                        _delay(0.8, 1.4)
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
                    el = self._page.get_by_text(label, exact=True).first
                    _mouse_to(self._page, el)
                    _delay(0.2, 0.5)
                    el.click(timeout=2000)
                    _delay(0.8, 1.5)
                    removed = True
                    break
                except PWTimeout:
                    continue

            if not removed:
                return False

            # Confirm dialog if it appears
            for label in ["Remove", "Confirm", "Yes"]:
                try:
                    el = self._page.locator(
                        f'[role="dialog"] >> text="{label}"'
                    ).first
                    _mouse_to(self._page, el)
                    _delay(0.2, 0.4)
                    el.click(timeout=2000)
                    _delay(0.8, 1.5)
                    break
                except PWTimeout:
                    continue

            _delay(1.0, 2.0)
            return True

        except Exception as exc:
            status(f"Error removing {name}: {exc}")
            return False
