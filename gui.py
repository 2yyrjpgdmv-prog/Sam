"""
tkinter GUI for the Facebook Group Activity Scanner.
The user logs in via a real visible browser window; all scraping and
member-removal is driven through that same browser session.
"""

import csv
import json
import os
import queue
import random
import threading
import time
from datetime import datetime
from tkinter import filedialog, messagebox
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, Dict, List, Optional, Set
import re

from fb_browser import FBBrowser, _group_id_from_input


ACTIVE_LIST_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "active_list.json"
)


# ── Browser worker ────────────────────────────────────────────────────────────

class BrowserWorker:
    """
    Single daemon thread that owns all browser interactions. GUI code pushes
    callables onto the queue via dispatch(); the worker runs them one at a
    time so the Playwright browser is never touched from multiple threads.
    """

    def __init__(self):
        self._q: "queue.Queue[Optional[Callable[[], Any]]]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while True:
            job = self._q.get()
            if job is None:
                return
            try:
                job()
            except Exception:
                pass

    def dispatch(self, job: Callable[[], Any]):
        self._q.put(job)

    def stop(self):
        self._q.put(None)


# ── Palette ───────────────────────────────────────────────────────────────────
FB_BLUE = "#1877f2"
FB_DARK = "#0d47a1"
GREEN   = "#42b72a"
RED     = "#e53935"
AMBER   = "#f9a825"
TEAL    = "#00838f"
LIGHT   = "#f0f2f5"
WHITE   = "#ffffff"
FONT    = "Segoe UI"


def _btn(parent, text, color, cmd, **kw):
    return tk.Button(
        parent, text=text, command=cmd,
        bg=color, fg=WHITE, font=(FONT, 9, "bold"),
        padx=12, pady=6, relief=tk.FLAT, cursor="hand2",
        activebackground=color, activeforeground=WHITE, **kw,
    )


# ── Removal schedule dialog ───────────────────────────────────────────────────

class RemovalScheduleDialog(tk.Toplevel):
    """
    Lets the user control how many members to remove and at what pace,
    so the activity looks natural to Facebook.
    """
    result = None  # set to dict on confirm, None on cancel

    def __init__(self, parent, total_selected: int):
        super().__init__(parent)
        self.title("Removal Schedule")
        self.geometry("460x340")
        self.resizable(False, False)
        self.configure(bg=WHITE)
        self.grab_set()
        self.result = None
        self._total = total_selected
        self._build(total_selected)

    def _build(self, total: int):
        pad = dict(padx=16, pady=6)

        tk.Label(self, text="Removal Schedule",
                 font=(FONT, 13, "bold"), bg=WHITE, fg="#222").pack(pady=(14, 2))
        tk.Label(self,
                 text=f"{total} inactive member(s) selected.\n"
                      "Set the pace below so the removals look natural.",
                 font=(FONT, 9), bg=WHITE, fg="#555", justify="center").pack()

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=14, pady=10)

        frm = tk.Frame(self, bg=WHITE)
        frm.pack(fill=tk.X, **pad)

        lbl = dict(bg=WHITE, font=(FONT, 9), anchor="w", width=28)
        spn = dict(font=(FONT, 9), width=6)

        # Session limit
        tk.Label(frm, text="Remove at most this many today:", **lbl).grid(
            row=0, column=0, sticky="w", pady=4)
        self._limit_var = tk.IntVar(value=min(total, 50))
        tk.Spinbox(frm, from_=1, to=total, textvariable=self._limit_var, **spn).grid(
            row=0, column=1, sticky="w", padx=8)
        tk.Label(frm, text="members", bg=WHITE, font=(FONT, 9)).grid(
            row=0, column=2, sticky="w")

        # Batch size
        tk.Label(frm, text="Remove in batches of:", **lbl).grid(
            row=1, column=0, sticky="w", pady=4)
        self._batch_var = tk.IntVar(value=10)
        tk.Spinbox(frm, from_=1, to=50, textvariable=self._batch_var, **spn).grid(
            row=1, column=1, sticky="w", padx=8)
        tk.Label(frm, text="members per batch", bg=WHITE, font=(FONT, 9)).grid(
            row=1, column=2, sticky="w")

        # Break duration
        tk.Label(frm, text="Take a break between batches of:", **lbl).grid(
            row=2, column=0, sticky="w", pady=4)
        self._break_var = tk.IntVar(value=10)
        tk.Spinbox(frm, from_=1, to=120, textvariable=self._break_var, **spn).grid(
            row=2, column=1, sticky="w", padx=8)
        tk.Label(frm, text="minutes", bg=WHITE, font=(FONT, 9)).grid(
            row=2, column=2, sticky="w")

        # Delay between individual removals
        tk.Label(frm, text="Delay between each removal:", **lbl).grid(
            row=3, column=0, sticky="w", pady=4)
        self._delay_var = tk.IntVar(value=30)
        tk.Spinbox(frm, from_=5, to=300, textvariable=self._delay_var, **spn).grid(
            row=3, column=1, sticky="w", padx=8)
        tk.Label(frm, text="seconds (± random jitter)", bg=WHITE, font=(FONT, 9)).grid(
            row=3, column=2, sticky="w")

        # Estimate label
        self._est_var = tk.StringVar()
        tk.Label(self, textvariable=self._est_var, bg=WHITE, fg="#1877f2",
                 font=(FONT, 9, "italic")).pack(pady=(4, 0))

        self._limit_var.trace_add("write", self._update_estimate)
        self._batch_var.trace_add("write", self._update_estimate)
        self._break_var.trace_add("write", self._update_estimate)
        self._delay_var.trace_add("write", self._update_estimate)
        self._update_estimate()

        # Buttons
        btn_row = tk.Frame(self, bg=WHITE)
        btn_row.pack(fill=tk.X, pady=12)
        _btn(btn_row, "Start Removing", RED, self._confirm).pack(
            side=tk.RIGHT, padx=14)
        _btn(btn_row, "Cancel", "#777", self.destroy).pack(
            side=tk.RIGHT, padx=4)

    def _update_estimate(self, *_):
        try:
            limit   = max(1, self._limit_var.get())
            batch   = max(1, self._batch_var.get())
            brk     = max(1, self._break_var.get())
            delay_s = max(1, self._delay_var.get())

            batches   = max(1, -(-limit // batch))          # ceiling division
            breaks    = max(0, batches - 1)
            total_s   = limit * delay_s + breaks * brk * 60
            h, rem    = divmod(total_s, 3600)
            m         = rem // 60
            if h:
                eta = f"~{h}h {m}m"
            else:
                eta = f"~{m}m"
            self._est_var.set(
                f"Estimated time: {eta}  "
                f"({batches} batch{'es' if batches > 1 else ''}, "
                f"{breaks} break{'s' if breaks != 1 else ''})"
            )
        except Exception:
            self._est_var.set("")

    def _confirm(self):
        try:
            self.result = {
                "limit":    max(1, self._limit_var.get()),
                "batch":    max(1, self._batch_var.get()),
                "break_s":  max(1, self._break_var.get()) * 60,
                "delay_s":  max(1, self._delay_var.get()),
            }
        except Exception:
            return
        self.destroy()


# ── Application ───────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Facebook Group Activity Scanner")
        self.geometry("980x730")
        self.minsize(780, 560)
        self.configure(bg=FB_BLUE)

        self._results: List[Dict] = []
        self._checked: Set[str]   = set()
        self._stop_event = threading.Event()
        self._login_confirmed = threading.Event()
        self._browser: Optional[FBBrowser] = None

        self._worker = BrowserWorker()
        self._active_list: Set[str] = set()
        self._active_list_group: str = ""
        self._active_list_updated: str = ""
        self._load_active_list()

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── Active list persistence ───────────────────────────────────────────────

    def _load_active_list(self):
        try:
            with open(ACTIVE_LIST_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._active_list = set(data.get("uids", []))
            self._active_list_group = data.get("group", "")
            self._active_list_updated = data.get("updated", "")
        except (OSError, ValueError):
            self._active_list = set()
            self._active_list_group = ""
            self._active_list_updated = ""

    def _save_active_list(self):
        data = {
            "group": self._active_list_group,
            "updated": self._active_list_updated,
            "uids": sorted(self._active_list),
        }
        try:
            with open(ACTIVE_LIST_FILE, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
        except OSError:
            pass

    def _active_list_status_text(self) -> str:
        if not self._active_list:
            return "Active list: empty — run Scan Feed to build it."
        parts = [f"Active list: {len(self._active_list)} user(s)"]
        if self._active_list_group:
            parts.append(f"group {self._active_list_group}")
        if self._active_list_updated:
            parts.append(f"updated {self._active_list_updated}")
        return " — ".join(parts)

    def _clear_active_list(self):
        if not self._active_list:
            return
        if not messagebox.askyesno(
            "Clear Active List",
            f"Discard the saved list of {len(self._active_list)} active user(s)?",
        ):
            return
        self._active_list = set()
        self._active_list_group = ""
        self._active_list_updated = ""
        try:
            if os.path.exists(ACTIVE_LIST_FILE):
                os.remove(ACTIVE_LIST_FILE)
        except OSError:
            pass
        if hasattr(self, "_active_status_var"):
            self._active_status_var.set(self._active_list_status_text())

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        self._build_header()
        self._build_config()
        self._build_login_row()
        self._build_action_row()
        self._build_progress()
        self._build_results()

    def _build_header(self):
        f = tk.Frame(self, bg=FB_BLUE)
        f.pack(fill=tk.X, padx=16, pady=(14, 4))
        tk.Label(f, text="Facebook Group Activity Scanner",
                 font=(FONT, 17, "bold"), fg=WHITE, bg=FB_BLUE).pack(anchor="w")
        tk.Label(f, text="Log in with your own Facebook account — "
                         "the app automates what you'd do manually as a group admin",
                 font=(FONT, 9), fg="#c8d8f8", bg=FB_BLUE).pack(anchor="w")

    def _build_config(self):
        f = tk.LabelFrame(self, text="  Configuration  ", padx=14, pady=10,
                          bg=WHITE, fg="#222", font=(FONT, 10, "bold"),
                          relief=tk.GROOVE)
        f.pack(fill=tk.X, padx=14, pady=6)

        lbl = dict(bg=WHITE, font=(FONT, 9), anchor="w", width=16)
        ent = dict(font=(FONT, 9))

        tk.Label(f, text="Group URL or ID:", **lbl).grid(
            row=0, column=0, sticky="w", pady=4)
        self._group_var = tk.StringVar()
        tk.Entry(f, textvariable=self._group_var, width=66, **ent).grid(
            row=0, column=1, sticky="ew", padx=6)
        tk.Label(f, text="e.g. facebook.com/groups/123… or just the ID",
                 font=(FONT, 8), fg="gray", bg=WHITE).grid(
            row=0, column=2, sticky="w", padx=4)

        tk.Label(f, text="Days to scan back:", **lbl).grid(
            row=1, column=0, sticky="w", pady=4)
        tf = tk.Frame(f, bg=WHITE)
        tf.grid(row=1, column=1, sticky="w", padx=6)
        self._days_var = tk.IntVar(value=7)
        tk.Spinbox(tf, from_=1, to=365, textvariable=self._days_var,
                   width=5, font=(FONT, 9)).pack(side=tk.LEFT)
        tk.Label(tf, text=" days of group feed to treat as 'recently active'",
                 font=(FONT, 9), bg=WHITE, fg="#555").pack(side=tk.LEFT)

        tk.Label(f, text="Daily removal limit:", **lbl).grid(
            row=2, column=0, sticky="w", pady=4)
        df = tk.Frame(f, bg=WHITE)
        df.grid(row=2, column=1, sticky="w", padx=6)
        self._daily_limit_var = tk.IntVar(value=50)
        tk.Spinbox(df, from_=10, to=200, textvariable=self._daily_limit_var,
                   width=5, font=(FONT, 9)).pack(side=tk.LEFT)
        tk.Label(df, text=" max members to scan as inactive before stopping",
                 font=(FONT, 9), bg=WHITE, fg="#555").pack(side=tk.LEFT)

        f.columnconfigure(1, weight=1)

    def _build_login_row(self):
        f = tk.Frame(self, bg=FB_DARK)
        f.pack(fill=tk.X, padx=14, pady=(0, 4))

        inner = tk.Frame(f, bg=FB_DARK)
        inner.pack(fill=tk.X, padx=12, pady=8)

        tk.Label(inner, text="Step 1 — Log in to Facebook:",
                 font=(FONT, 9, "bold"), fg=WHITE, bg=FB_DARK).pack(side=tk.LEFT)

        self._login_btn = _btn(inner, "Open Browser & Log In",
                               GREEN, self._open_browser)
        self._login_btn.pack(side=tk.LEFT, padx=10)

        # Shown while waiting — lets the user confirm login manually
        self._confirm_login_btn = _btn(inner, "I'm Logged In ✓", "#27ae60",
                                       self._confirm_login)
        # hidden until browser is waiting
        self._confirm_login_btn.pack(side=tk.LEFT, padx=4)
        self._confirm_login_btn.pack_forget()

        self._logout_btn = _btn(inner, "Clear Saved Login", "#c0392b",
                                self._clear_session)
        self._logout_btn.pack(side=tk.LEFT, padx=4)

        self._login_status_var = tk.StringVar(
            value="Not logged in — click the button above to open a browser window.")
        tk.Label(inner, textvariable=self._login_status_var,
                 font=(FONT, 9), fg="#ffd", bg=FB_DARK).pack(side=tk.LEFT, padx=8)

    def _build_action_row(self):
        f = tk.Frame(self, bg=FB_BLUE)
        f.pack(fill=tk.X, padx=14, pady=4)

        tk.Label(f, text="Step 2 — Scan & manage:",
                 font=(FONT, 9, "bold"), fg=WHITE, bg=FB_BLUE).pack(side=tk.LEFT, padx=(0, 8))

        self._scan_btn = _btn(f, "▶  Scan Group", FB_DARK, self._start_scan,
                              state=tk.DISABLED)
        self._scan_btn.pack(side=tk.LEFT)

        self._stop_btn = _btn(f, "⏹  Stop", AMBER, self._stop_action,
                              state=tk.DISABLED)
        self._stop_btn.pack(side=tk.LEFT, padx=6)

        self._sel_btn = _btn(f, "Select All Inactive", "#555",
                             self._select_all_inactive, state=tk.DISABLED)
        self._sel_btn.pack(side=tk.LEFT)

        self._desel_btn = _btn(f, "Deselect All", "#777",
                               self._deselect_all, state=tk.DISABLED)
        self._desel_btn.pack(side=tk.LEFT, padx=6)

        self._remove_btn = _btn(f, "🗑  Remove Selected", RED,
                                self._remove_selected, state=tk.DISABLED)
        self._remove_btn.pack(side=tk.LEFT)

        self._export_btn = _btn(f, "💾  Export CSV", TEAL,
                                self._export_csv, state=tk.DISABLED)
        self._export_btn.pack(side=tk.LEFT, padx=8)

    def _build_progress(self):
        f = tk.Frame(self, bg=FB_BLUE)
        f.pack(fill=tk.X, padx=14, pady=(4, 0))

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("g.Horizontal.TProgressbar",
                        troughcolor=FB_DARK, background=GREEN, thickness=10)

        self._progress_var = tk.DoubleVar()
        ttk.Progressbar(f, variable=self._progress_var, maximum=100,
                        style="g.Horizontal.TProgressbar").pack(fill=tk.X)

        self._status_var = tk.StringVar(
            value="Open a browser and log in to Facebook to get started.")
        tk.Label(self, textvariable=self._status_var, bg=FB_BLUE, fg=WHITE,
                 font=(FONT, 9), anchor="w").pack(fill=tk.X, padx=14, pady=(2, 5))

    def _build_results(self):
        f = tk.LabelFrame(self, text="  Group Members  ", bg=WHITE, fg="#222",
                          font=(FONT, 10, "bold"), relief=tk.GROOVE)
        f.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 10))

        # Filter bar
        bar = tk.Frame(f, bg=WHITE)
        bar.pack(fill=tk.X, padx=8, pady=5)

        self._filter_var = tk.StringVar(value="all")
        for label, val in [("All", "all"), ("Active ✓", "active"),
                           ("Inactive ✗", "inactive")]:
            tk.Radiobutton(bar, text=label, variable=self._filter_var,
                           value=val, bg=WHITE, font=(FONT, 9),
                           command=self._refresh_table,
                           cursor="hand2").pack(side=tk.LEFT, padx=6)

        self._summary_var = tk.StringVar(value="")
        tk.Label(bar, textvariable=self._summary_var, bg=WHITE, fg="#555",
                 font=(FONT, 9)).pack(side=tk.RIGHT, padx=10)

        ttk.Separator(f, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=6, pady=(0, 2))

        # Colour legend
        legend = tk.Frame(f, bg=WHITE)
        legend.pack(fill=tk.X, padx=8, pady=(0, 3))
        for colour, label in [
            ("#e8f5e9", "Active"),
            ("#ffebee", "Inactive"),
            ("#e3f2fd", "Admin (protected)"),
            ("#fff9c4", "Selected for removal"),
        ]:
            tk.Frame(legend, bg=colour, width=14, height=14,
                     relief=tk.GROOVE).pack(side=tk.LEFT)
            tk.Label(legend, text=f" {label}  ", bg=WHITE,
                     font=(FONT, 8), fg="#444").pack(side=tk.LEFT)

        # Click-to-toggle checkbox column note
        tk.Label(f, text="Click the ☑ column to toggle selection",
                 bg=WHITE, fg="#888", font=(FONT, 8), anchor="e").pack(
            fill=tk.X, padx=10)

        # Treeview
        cols = ("chk", "name", "uid", "status", "admin")
        self._tree = ttk.Treeview(f, columns=cols, show="headings",
                                  selectmode="none")

        for cid, heading, width, anchor, stretch in [
            ("chk",    "☑",         52,  "center", False),
            ("name",   "Name",      240, "w",      True),
            ("uid",    "Profile ID", 170, "center", False),
            ("status", "Status",    115, "center", False),
            ("admin",  "Admin",     70,  "center", False),
        ]:
            self._tree.heading(cid, text=heading)
            self._tree.column(cid, width=width, anchor=anchor,
                              stretch=tk.YES if stretch else tk.NO)

        vsb = ttk.Scrollbar(f, orient="vertical",   command=self._tree.yview)
        hsb = ttk.Scrollbar(f, orient="horizontal", command=self._tree.xview)
        self._tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self._tree.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 2))

        self._tree.tag_configure("active",   background="#e8f5e9", foreground="#1b5e20")
        self._tree.tag_configure("inactive", background="#ffebee", foreground="#b71c1c")
        self._tree.tag_configure("admin",    background="#e3f2fd", foreground="#0d47a1")
        self._tree.tag_configure("checked",  background="#fff9c4", foreground="#333")

        self._tree.bind("<Button-1>", self._on_tree_click)

    # ── Login flow ────────────────────────────────────────────────────────────

    def _open_browser(self):
        self._login_btn.config(state=tk.DISABLED)
        self._login_confirmed.clear()
        self._login_status_var.set("Opening browser…")

        def worker():
            try:
                if self._browser:
                    self._browser.close()
                self._browser = FBBrowser()
                already_in = self._browser.launch()

                if already_in:
                    # Session still valid — skip login entirely
                    name = self._browser.logged_in_as
                    self.after(0, lambda n=name: self._on_login_success(n))
                    return

                # Need to log in — show the browser and wait
                self.after(0, lambda: self._login_status_var.set(
                    "Log in to Facebook in the browser window, "
                    "then click \"I'm Logged In\"."
                ))
                self.after(0, lambda: self._confirm_login_btn.pack(
                    side=tk.LEFT, padx=4, before=self._logout_btn
                ))
                ok = self._browser.wait_for_login(
                    timeout_s=600,
                    on_poll=lambda m: self.after(
                        0, lambda msg=m: self._login_status_var.set(msg)
                    ),
                    confirm_event=self._login_confirmed,
                )
                self.after(0, self._confirm_login_btn.pack_forget)
                if ok:
                    name = self._browser.logged_in_as
                    self.after(0, lambda n=name: self._on_login_success(n))
                else:
                    self.after(0, self._on_login_timeout)
            except Exception as exc:
                msg = str(exc)
                self.after(0, self._confirm_login_btn.pack_forget)
                self.after(0, lambda m=msg: self._on_login_error(m))

        threading.Thread(target=worker, daemon=True).start()

    def _confirm_login(self):
        """User clicked 'I'm Logged In' — signal the waiting thread."""
        self._login_confirmed.set()
        self._login_status_var.set("Confirming login…")

    def _on_login_success(self, name: str):
        self._login_status_var.set(f"Logged in as: {name}  ✓")
        self._login_btn.config(text="Re-open Browser", state=tk.NORMAL)
        self._scan_btn.config(state=tk.NORMAL)
        self._status_var.set(f"Logged in as {name}. Enter a group URL/ID and click Scan Group.")

    def _on_login_timeout(self):
        self._login_status_var.set("Login timed out. Try again.")
        self._login_btn.config(state=tk.NORMAL)

    def _on_login_error(self, msg: str):
        self._login_status_var.set(f"Error: {msg}")
        self._login_btn.config(state=tk.NORMAL)
        messagebox.showerror(
            "Browser Error",
            f"{msg}\n\nMake sure you have run install.bat at least once.",
        )

    def _clear_session(self):
        """Delete the saved login session so the user is prompted to log in again."""
        if self._browser:
            self._browser.clear_session()
        else:
            # Browser not open yet — delete the file directly
            import os
            from fb_browser import SESSION_FILE
            try:
                if os.path.exists(SESSION_FILE):
                    os.remove(SESSION_FILE)
            except Exception:
                pass
        self._login_status_var.set(
            "Saved login cleared — click Open Browser & Log In to log in again."
        )
        self._scan_btn.config(state=tk.DISABLED)

    # ── Scan flow ─────────────────────────────────────────────────────────────

    def _start_scan(self):
        group_raw = self._group_var.get().strip()
        if not group_raw:
            messagebox.showwarning("No Group",
                                   "Enter a Facebook Group URL or ID first.")
            return
        if not self._browser:
            messagebox.showwarning("Not Logged In",
                                   "Open a browser and log in first.")
            return

        group_id = _group_id_from_input(group_raw)
        days = self._days_var.get()

        self._results.clear()
        self._checked.clear()
        self._refresh_table()
        self._progress_var.set(0)
        self._stop_event.clear()
        self._set_busy(True)

        threading.Thread(
            target=self._scan_worker,
            args=(group_id, days),
            daemon=True,
        ).start()

    def _scan_worker(self, group_id: str, days: int):
        def status(m):
            self.after(0, lambda msg=m: self._status_var.set(msg))

        def progress(pct):
            self.after(0, lambda p=pct: self._progress_var.set(p))

        try:
            # ── Members ───────────────────────────────────────────────────────
            status("Fetching group members…")
            members = self._browser.scrape_members(
                group_id,
                on_status=status,
                stop_event=self._stop_event,
            )
            if self._stop_event.is_set():
                self.after(0, self._on_stopped)
                return
            progress(40)
            status(f"Found {len(members)} members. Now scanning the group feed…")

            # ── Activity ──────────────────────────────────────────────────────
            active_ids = self._browser.scrape_active_users(
                group_id,
                days,
                on_status=status,
                stop_event=self._stop_event,
            )
            if self._stop_event.is_set():
                self.after(0, self._on_stopped)
                return
            progress(95)

            # ── Merge ─────────────────────────────────────────────────────────
            for m in members:
                m["active"] = m["id"] in active_ids

            # Inactive first, then alphabetical
            members.sort(key=lambda r: (r["active"], r["name"].lower()))

            progress(100)
            self.after(0, lambda r=members: self._on_scan_complete(r))

        except Exception as exc:
            msg = str(exc)
            self.after(0, lambda m=msg: self._on_error(m))

    def _on_scan_complete(self, results: list):
        self._results = results
        self._set_busy(False)
        self._sel_btn.config(state=tk.NORMAL)
        self._desel_btn.config(state=tk.NORMAL)
        self._export_btn.config(state=tk.NORMAL)
        self._refresh_table()
        active   = sum(1 for r in results if r["active"])
        inactive = len(results) - active
        self._status_var.set(
            f"Scan complete — {len(results)} members: "
            f"{active} active, {inactive} inactive."
        )

    def _on_stopped(self):
        self._set_busy(False)
        self._status_var.set("Stopped.")

    def _on_error(self, msg: str):
        self._set_busy(False)
        self._status_var.set(f"Error: {msg}")
        messagebox.showerror("Error", msg)

    def _stop_action(self):
        self._stop_event.set()
        self._stop_btn.config(state=tk.DISABLED)
        self._status_var.set("Stopping…")

    def _set_busy(self, busy: bool):
        self._scan_btn.config(state=tk.DISABLED if busy else tk.NORMAL)
        self._stop_btn.config(state=tk.NORMAL if busy else tk.DISABLED)

    # ── Table ─────────────────────────────────────────────────────────────────

    def _refresh_table(self):
        self._tree.delete(*self._tree.get_children())

        fil = self._filter_var.get()
        shown = [
            r for r in self._results
            if fil == "all"
            or (fil == "active"   and r["active"])
            or (fil == "inactive" and not r["active"])
        ]

        total    = len(self._results)
        active   = sum(1 for r in self._results if r["active"])
        inactive = total - active
        sel      = len(self._checked)
        self._summary_var.set(
            f"Total: {total}  |  Active: {active}  |  "
            f"Inactive: {inactive}  |  Selected: {sel}"
        )

        for r in shown:
            checked = r["id"] in self._checked
            tag = (
                "checked"  if checked       else
                "admin"    if r["admin"]    else
                "active"   if r["active"]   else
                "inactive"
            )
            self._tree.insert(
                "", tk.END, iid=r["id"],
                values=(
                    "☑" if checked else "☐",
                    r["name"],
                    r["id"],
                    "Active ✓" if r["active"] else "Inactive ✗",
                    "Yes" if r["admin"] else "",
                ),
                tags=(tag,),
            )

        removable = [
            r for r in self._results
            if r["id"] in self._checked and not r["active"] and not r["admin"]
        ]
        self._remove_btn.config(
            state=tk.NORMAL if removable else tk.DISABLED
        )

    def _on_tree_click(self, event):
        col = self._tree.identify_column(event.x)
        row = self._tree.identify_row(event.y)
        if not row or col != "#1":
            return
        member = next((r for r in self._results if r["id"] == row), None)
        if not member or member["admin"]:
            return
        if row in self._checked:
            self._checked.discard(row)
        else:
            self._checked.add(row)
        self._refresh_table()

    def _select_all_inactive(self):
        for r in self._results:
            if not r["active"] and not r["admin"]:
                self._checked.add(r["id"])
        self._refresh_table()

    def _deselect_all(self):
        self._checked.clear()
        self._refresh_table()

    # ── Remove ────────────────────────────────────────────────────────────────

    def _remove_selected(self):
        removable = [
            r for r in self._results
            if r["id"] in self._checked and not r["active"] and not r["admin"]
        ]
        if not removable:
            messagebox.showinfo("Nothing Selected",
                                "Select some inactive non-admin members first.")
            return

        group_raw = self._group_var.get().strip()
        group_id  = _group_id_from_input(group_raw)

        # Show schedule dialog — user picks pace
        dlg = RemovalScheduleDialog(self, total_selected=len(removable))
        self.wait_window(dlg)
        if dlg.result is None:
            return  # cancelled

        sched = dlg.result
        # Cap the list to session limit
        members_to_remove = removable[: sched["limit"]]

        self._set_busy(True)
        self._remove_btn.config(state=tk.DISABLED)
        self._stop_event.clear()
        self._progress_var.set(0)

        threading.Thread(
            target=self._remove_worker,
            args=(group_id, members_to_remove, sched),
            daemon=True,
        ).start()

    def _remove_worker(self, group_id: str, members: list, sched: dict):
        """
        Remove members in batches.  Between batches, pause for sched['break_s']
        seconds while showing a live countdown.  Between individual removals,
        sleep sched['delay_s'] ± 30% random jitter.
        """
        batch_size = sched["batch"]
        break_s    = sched["break_s"]
        delay_s    = sched["delay_s"]
        total      = len(members)

        removed_ids: list = []
        failed = 0
        done = 0

        def set_status(msg):
            self.after(0, lambda s=msg: self._status_var.set(s))

        def set_progress(pct):
            self.after(0, lambda p=pct: self._progress_var.set(p))

        batches = [members[i: i + batch_size]
                   for i in range(0, total, batch_size)]

        for b_idx, batch in enumerate(batches):
            if self._stop_event.is_set():
                break

            set_status(
                f"Batch {b_idx + 1}/{len(batches)} — "
                f"removing {len(batch)} member(s)…"
            )

            for m in batch:
                if self._stop_event.is_set():
                    break

                ok = self._browser.remove_member(
                    group_id, m,
                    on_status=set_status,
                )
                if ok:
                    removed_ids.append(m["id"])
                else:
                    failed += 1

                done += 1
                set_progress(100 * done / total)

                # Per-removal delay with ±30 % jitter
                if not self._stop_event.is_set():
                    jitter = delay_s * random.uniform(0.7, 1.3)
                    time.sleep(jitter)

            # Break between batches (skip after the last one)
            if b_idx < len(batches) - 1 and not self._stop_event.is_set():
                for remaining in range(break_s, 0, -1):
                    if self._stop_event.is_set():
                        break
                    m_left, s_left = divmod(remaining, 60)
                    set_status(
                        f"Break after batch {b_idx + 1}/{len(batches)} — "
                        f"resuming in {m_left}:{s_left:02d}…  "
                        f"({done}/{total} removed so far)"
                    )
                    time.sleep(1)

        self.after(0, lambda: self._on_remove_done(removed_ids, failed))

    def _on_remove_done(self, removed_ids: list, failed: int):
        removed_set = set(removed_ids)
        self._results  = [r for r in self._results  if r["id"] not in removed_set]
        self._checked -= removed_set

        self._set_busy(False)
        self._refresh_table()
        self._status_var.set(
            f"Done — {len(removed_ids)} removed, {failed} failed."
        )
        if failed:
            messagebox.showwarning(
                "Partial Failure",
                f"{len(removed_ids)} removed.\n"
                f"{failed} could not be removed — Facebook may have changed "
                f"its layout, or you may not have admin rights for those members.",
            )
        else:
            messagebox.showinfo("Done",
                                f"{len(removed_ids)} members removed successfully.")

    # ── Export ────────────────────────────────────────────────────────────────

    def _export_csv(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            title="Save member report",
        )
        if not path:
            return

        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["name", "id", "status", "admin"])
            w.writeheader()
            for r in self._results:
                w.writerow({
                    "name":   r["name"],
                    "id":     r["id"],
                    "status": "Active" if r["active"] else "Inactive",
                    "admin":  "Yes" if r["admin"] else "No",
                })
        self._status_var.set(f"Exported {len(self._results)} members → {path}")

    # ── Window close ──────────────────────────────────────────────────────────

    def _on_close(self):
        self._stop_event.set()
        if self._browser:
            threading.Thread(target=self._browser.close, daemon=True).start()
        self.destroy()
