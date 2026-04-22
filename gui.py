"""
Main GUI — tkinter-based Windows desktop application.
All Facebook API calls run on background threads; UI updates use .after().
"""

import csv
import threading
import time
import webbrowser
from tkinter import filedialog, messagebox
import tkinter as tk
from tkinter import ttk
from typing import Dict, List, Optional

from facebook_api import FacebookAPI, FacebookAPIError
from scanner import ActivityScanner


# ── Palette ───────────────────────────────────────────────────────────────────
FB_BLUE = "#1877f2"
FB_DARK = "#0d47a1"
GREEN = "#42b72a"
RED = "#e53935"
AMBER = "#f9a825"
TEAL = "#00838f"
LIGHT = "#f0f2f5"
WHITE = "#ffffff"

FONT = "Segoe UI"


def _btn(parent, text, color, command, **kw):
    return tk.Button(
        parent,
        text=text,
        command=command,
        bg=color,
        fg=WHITE,
        font=(FONT, 9, "bold"),
        padx=12,
        pady=6,
        relief=tk.FLAT,
        cursor="hand2",
        activebackground=color,
        activeforeground=WHITE,
        **kw,
    )


# ── Help dialog ───────────────────────────────────────────────────────────────

TOKEN_HELP = """How to get a Facebook Access Token
══════════════════════════════════════

OPTION A – Graph API Explorer (quickest)
─────────────────────────────────────────
1. Go to:  developers.facebook.com/tools/explorer
2. Select (or create) a Facebook App.
3. Click "Generate Access Token".
4. Add these permissions if prompted:
     • groups_access_member_info
     • publish_to_groups   (needed to remove members)
5. Copy the token and paste it here.

Note: tokens from the Explorer expire in ~1 hour.
For longer sessions generate a Long-Lived Token.

OPTION B – Long-Lived Token
─────────────────────────────
Exchange a short-lived token via:
  graph.facebook.com/oauth/access_token
    ?grant_type=fb_exchange_token
    &client_id=YOUR_APP_ID
    &client_secret=YOUR_APP_SECRET
    &fb_exchange_token=SHORT_LIVED_TOKEN

IMPORTANT
─────────
• You must be an admin of the group you want to scan.
• The Graph API only returns member activity that is
  accessible to your app.  Some data may be limited
  by Facebook's privacy settings.
• Automated bulk-removal is governed by Facebook's
  Platform Policy – use responsibly.
"""


class HelpDialog(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("How to get your Access Token")
        self.geometry("560x440")
        self.resizable(False, False)
        self.configure(bg=WHITE)
        self.grab_set()

        txt = tk.Text(self, wrap=tk.WORD, font=(FONT, 9), bg=WHITE, relief=tk.FLAT,
                      padx=12, pady=10)
        txt.insert(tk.END, TOKEN_HELP)
        txt.config(state=tk.DISABLED)
        txt.pack(fill=tk.BOTH, expand=True)

        btn_row = tk.Frame(self, bg=WHITE)
        btn_row.pack(fill=tk.X, pady=8)
        _btn(btn_row, "Open Graph API Explorer",
             FB_BLUE,
             lambda: webbrowser.open("https://developers.facebook.com/tools/explorer"),
             ).pack(side=tk.LEFT, padx=12)
        _btn(btn_row, "Close", "#555", self.destroy).pack(side=tk.RIGHT, padx=12)


# ── Main application ──────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Facebook Group Activity Scanner")
        self.geometry("980x720")
        self.minsize(780, 560)
        self.configure(bg=FB_BLUE)

        self._results: List[Dict] = []
        self._checked: set = set()          # user IDs marked for removal
        self._stop_event = threading.Event()
        self._token_visible = False

        self._build_ui()

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        self._build_header()
        self._build_config()
        self._build_buttons()
        self._build_progress()
        self._build_results()

    def _build_header(self):
        frm = tk.Frame(self, bg=FB_BLUE)
        frm.pack(fill=tk.X, padx=16, pady=(14, 4))
        tk.Label(frm, text="Facebook Group Activity Scanner",
                 font=(FONT, 18, "bold"), fg=WHITE, bg=FB_BLUE).pack(anchor="w")
        tk.Label(frm, text="Identify inactive members and remove them in bulk",
                 font=(FONT, 9), fg="#c8d8f8", bg=FB_BLUE).pack(anchor="w")

    def _build_config(self):
        frm = tk.LabelFrame(self, text="  Configuration  ", padx=14, pady=10,
                            bg=WHITE, fg="#222", font=(FONT, 10, "bold"),
                            relief=tk.GROOVE)
        frm.pack(fill=tk.X, padx=14, pady=6)

        lbl = dict(bg=WHITE, font=(FONT, 9), anchor="w", width=16)
        ent = dict(font=(FONT, 9))

        # Access token
        tk.Label(frm, text="Access Token:", **lbl).grid(row=0, column=0, sticky="w", pady=4)
        self._token_var = tk.StringVar()
        self._token_entry = tk.Entry(frm, textvariable=self._token_var,
                                     show="•", width=64, **ent)
        self._token_entry.grid(row=0, column=1, sticky="ew", padx=6)
        tk.Button(frm, text="Show", command=self._toggle_token,
                  bg=LIGHT, font=(FONT, 8), relief=tk.FLAT, width=5,
                  cursor="hand2").grid(row=0, column=2, padx=4)
        tk.Button(frm, text="?", command=lambda: HelpDialog(self),
                  bg=FB_BLUE, fg=WHITE, font=(FONT, 8, "bold"), relief=tk.FLAT,
                  width=3, cursor="hand2").grid(row=0, column=3, padx=2)

        # Group ID
        tk.Label(frm, text="Group ID:", **lbl).grid(row=1, column=0, sticky="w", pady=4)
        self._group_var = tk.StringVar()
        tk.Entry(frm, textvariable=self._group_var, width=64, **ent).grid(
            row=1, column=1, sticky="ew", padx=6)
        tk.Label(frm, text="(numbers from the group URL)",
                 font=(FONT, 8), fg="gray", bg=WHITE).grid(row=1, column=2,
                                                            columnspan=2, sticky="w")

        # Inactivity threshold
        tk.Label(frm, text="Inactive after:", **lbl).grid(row=2, column=0, sticky="w", pady=4)
        tf = tk.Frame(frm, bg=WHITE)
        tf.grid(row=2, column=1, sticky="w", padx=6)
        self._months_var = tk.IntVar(value=3)
        tk.Spinbox(tf, from_=1, to=24, textvariable=self._months_var,
                   width=4, font=(FONT, 9)).pack(side=tk.LEFT)
        tk.Label(tf, text=" months without posting, commenting, or reacting",
                 font=(FONT, 9), bg=WHITE, fg="#555").pack(side=tk.LEFT)

        frm.columnconfigure(1, weight=1)

    def _build_buttons(self):
        frm = tk.Frame(self, bg=FB_BLUE)
        frm.pack(fill=tk.X, padx=14, pady=4)

        self._scan_btn = _btn(frm, "▶  Scan Group", GREEN, self._start_scan)
        self._scan_btn.pack(side=tk.LEFT)

        self._stop_btn = _btn(frm, "⏹  Stop", AMBER, self._stop_scan, state=tk.DISABLED)
        self._stop_btn.pack(side=tk.LEFT, padx=8)

        self._sel_btn = _btn(frm, "Select All Inactive", "#555",
                             self._select_all_inactive, state=tk.DISABLED)
        self._sel_btn.pack(side=tk.LEFT)

        self._desel_btn = _btn(frm, "Deselect All", "#777",
                               self._deselect_all, state=tk.DISABLED)
        self._desel_btn.pack(side=tk.LEFT, padx=6)

        self._remove_btn = _btn(frm, "🗑  Remove Selected", RED,
                                self._remove_selected, state=tk.DISABLED)
        self._remove_btn.pack(side=tk.LEFT)

        self._export_btn = _btn(frm, "💾  Export CSV", TEAL,
                                self._export_csv, state=tk.DISABLED)
        self._export_btn.pack(side=tk.LEFT, padx=8)

    def _build_progress(self):
        frm = tk.Frame(self, bg=FB_BLUE)
        frm.pack(fill=tk.X, padx=14, pady=(4, 0))

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("green.Horizontal.TProgressbar",
                        troughcolor=FB_DARK, background=GREEN, thickness=10)

        self._progress_var = tk.DoubleVar()
        ttk.Progressbar(frm, variable=self._progress_var, maximum=100,
                        style="green.Horizontal.TProgressbar").pack(fill=tk.X)

        self._status_var = tk.StringVar(
            value="Ready — enter your access token and group ID, then click Scan Group.")
        tk.Label(self, textvariable=self._status_var, bg=FB_BLUE, fg=WHITE,
                 font=(FONT, 9), anchor="w").pack(fill=tk.X, padx=14, pady=(2, 6))

    def _build_results(self):
        frm = tk.LabelFrame(self, text="  Group Members  ", bg=WHITE, fg="#222",
                            font=(FONT, 10, "bold"), relief=tk.GROOVE)
        frm.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 10))

        # Filter / summary bar
        bar = tk.Frame(frm, bg=WHITE)
        bar.pack(fill=tk.X, padx=8, pady=5)

        self._filter_var = tk.StringVar(value="all")
        for label, val in [("All", "all"), ("Active ✓", "active"), ("Inactive ✗", "inactive")]:
            tk.Radiobutton(bar, text=label, variable=self._filter_var, value=val,
                           bg=WHITE, font=(FONT, 9), command=self._refresh_table,
                           cursor="hand2").pack(side=tk.LEFT, padx=6)

        self._summary_var = tk.StringVar(value="")
        tk.Label(bar, textvariable=self._summary_var, bg=WHITE, fg="#555",
                 font=(FONT, 9)).pack(side=tk.RIGHT, padx=10)

        ttk.Separator(frm, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=6, pady=(0, 2))

        # Legend
        legend = tk.Frame(frm, bg=WHITE)
        legend.pack(fill=tk.X, padx=8, pady=(0, 3))
        for colour, label in [("#e8f5e9", "Active"), ("#ffebee", "Inactive"),
                               ("#e3f2fd", "Admin"), ("#fff9c4", "Selected for removal")]:
            box = tk.Frame(legend, bg=colour, width=14, height=14, relief=tk.GROOVE)
            box.pack(side=tk.LEFT)
            tk.Label(legend, text=f" {label}  ", bg=WHITE, font=(FONT, 8),
                     fg="#444").pack(side=tk.LEFT)

        # Treeview
        cols = ("chk", "name", "uid", "status", "admin")
        self._tree = ttk.Treeview(frm, columns=cols, show="headings", selectmode="none")

        for cid, heading, width, anchor, stretch in [
            ("chk",    "☑",         52,  "center", False),
            ("name",   "Name",      230, "w",      True),
            ("uid",    "User ID",   165, "center", False),
            ("status", "Status",    115, "center", False),
            ("admin",  "Admin",     70,  "center", False),
        ]:
            self._tree.heading(cid, text=heading)
            self._tree.column(cid, width=width, anchor=anchor,
                              stretch=tk.YES if stretch else tk.NO)

        vsb = ttk.Scrollbar(frm, orient="vertical", command=self._tree.yview)
        hsb = ttk.Scrollbar(frm, orient="horizontal", command=self._tree.xview)
        self._tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self._tree.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 2))

        self._tree.tag_configure("active",   background="#e8f5e9", foreground="#1b5e20")
        self._tree.tag_configure("inactive", background="#ffebee", foreground="#b71c1c")
        self._tree.tag_configure("admin",    background="#e3f2fd", foreground="#0d47a1")
        self._tree.tag_configure("checked",  background="#fff9c4", foreground="#333")

        self._tree.bind("<Button-1>", self._on_tree_click)

    # ── Token visibility ──────────────────────────────────────────────────────

    def _toggle_token(self):
        self._token_visible = not self._token_visible
        self._token_entry.config(show="" if self._token_visible else "•")

    # ── Scan ──────────────────────────────────────────────────────────────────

    def _start_scan(self):
        token = self._token_var.get().strip()
        group_id = self._group_var.get().strip()

        if not token:
            messagebox.showwarning("Missing Token",
                                   "Please enter your Facebook access token.\n"
                                   "Click the ? button for instructions.")
            return
        if not group_id:
            messagebox.showwarning("Missing Group ID",
                                   "Please enter the Facebook Group ID (numbers only).")
            return

        self._results.clear()
        self._checked.clear()
        self._refresh_table()
        self._progress_var.set(0)
        self._stop_event.clear()
        self._set_scanning(True)

        api = FacebookAPI(token)
        scanner = ActivityScanner(api, group_id, self._months_var.get())

        threading.Thread(target=self._scan_worker, args=(scanner,), daemon=True).start()

    def _scan_worker(self, scanner: ActivityScanner):
        def progress(pct):
            self.after(0, lambda p=pct: self._progress_var.set(p))

        def status(msg):
            self.after(0, lambda m=msg: self._status_var.set(m))

        try:
            results = scanner.scan(
                on_progress=progress,
                on_status=status,
                stop_event=self._stop_event,
            )
            if results is None:
                self.after(0, self._on_scan_stopped)
            else:
                self.after(0, lambda r=results: self._on_scan_complete(r))
        except FacebookAPIError as exc:
            msg = str(exc)
            self.after(0, lambda m=msg: self._on_scan_error(m))
        except Exception as exc:
            msg = str(exc)
            self.after(0, lambda m=msg: self._on_scan_error(m))

    def _on_scan_complete(self, results: list):
        self._results = results
        self._set_scanning(False)
        self._sel_btn.config(state=tk.NORMAL)
        self._desel_btn.config(state=tk.NORMAL)
        self._export_btn.config(state=tk.NORMAL)
        self._refresh_table()
        active = sum(1 for r in results if r["active"])
        inactive = len(results) - active
        self._status_var.set(
            f"Scan complete — {len(results)} members: "
            f"{active} active, {inactive} inactive."
        )

    def _on_scan_stopped(self):
        self._set_scanning(False)
        self._status_var.set("Scan stopped by user.")

    def _on_scan_error(self, msg: str):
        self._set_scanning(False)
        self._status_var.set(f"Error: {msg}")
        messagebox.showerror(
            "Scan Error",
            f"{msg}\n\nCommon causes:\n"
            "• Invalid or expired access token\n"
            "• Missing groups_access_member_info permission\n"
            "• You are not an admin of this group\n"
            "• Wrong Group ID (use the numeric ID, not the URL slug)",
        )

    def _stop_scan(self):
        self._stop_event.set()
        self._stop_btn.config(state=tk.DISABLED)
        self._status_var.set("Stopping scan…")

    def _set_scanning(self, scanning: bool):
        self._scan_btn.config(state=tk.DISABLED if scanning else tk.NORMAL)
        self._stop_btn.config(state=tk.NORMAL if scanning else tk.DISABLED)

    # ── Table helpers ─────────────────────────────────────────────────────────

    def _refresh_table(self):
        self._tree.delete(*self._tree.get_children())

        fil = self._filter_var.get()
        shown = [
            r for r in self._results
            if fil == "all"
            or (fil == "active" and r["active"])
            or (fil == "inactive" and not r["active"])
        ]

        total = len(self._results)
        active = sum(1 for r in self._results if r["active"])
        inactive = total - active
        sel = len(self._checked)
        self._summary_var.set(
            f"Total: {total}  |  Active: {active}  |  "
            f"Inactive: {inactive}  |  Selected: {sel}"
        )

        for r in shown:
            checked = r["id"] in self._checked
            chk = "☑" if checked else "☐"
            status_txt = "Active ✓" if r["active"] else "Inactive ✗"
            admin_txt = "Yes" if r["admin"] else ""

            if checked:
                tag = "checked"
            elif r["admin"]:
                tag = "admin"
            elif r["active"]:
                tag = "active"
            else:
                tag = "inactive"

            self._tree.insert(
                "", tk.END, iid=r["id"],
                values=(chk, r["name"], r["id"], status_txt, admin_txt),
                tags=(tag,),
            )

        # Only enable remove when non-admin inactive members are checked
        removable = [
            r for r in self._results
            if r["id"] in self._checked and not r["active"] and not r["admin"]
        ]
        self._remove_btn.config(state=tk.NORMAL if removable else tk.DISABLED)

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
        token = self._token_var.get().strip()
        group_id = self._group_var.get().strip()

        removable = [
            r for r in self._results
            if r["id"] in self._checked and not r["active"] and not r["admin"]
        ]
        if not removable:
            messagebox.showinfo("Nothing to Remove",
                                "No inactive non-admin members are selected.")
            return

        if not messagebox.askyesno(
            "Confirm Removal",
            f"Remove {len(removable)} inactive member(s) from the group?\n\n"
            "This action cannot be undone.\n\nContinue?",
            icon="warning",
        ):
            return

        self._set_scanning(True)
        self._remove_btn.config(state=tk.DISABLED)
        self._stop_event.clear()
        self._progress_var.set(0)

        api = FacebookAPI(token)
        threading.Thread(
            target=self._remove_worker,
            args=(api, group_id, removable),
            daemon=True,
        ).start()

    def _remove_worker(self, api: FacebookAPI, group_id: str, members: list):
        removed_ids = []
        failed = 0
        total = len(members)

        for i, m in enumerate(members):
            if self._stop_event.is_set():
                break

            name = m["name"]
            self.after(0, lambda n=name, i=i: self._status_var.set(
                f"Removing {i + 1}/{total}: {n}…"
            ))

            try:
                api.remove_member(group_id, m["id"])
                removed_ids.append(m["id"])
            except Exception:
                failed += 1

            pct = 100 * (i + 1) / total
            self.after(0, lambda p=pct: self._progress_var.set(p))
            time.sleep(0.35)  # stay within rate limits

        self.after(0, lambda: self._on_remove_complete(removed_ids, failed))

    def _on_remove_complete(self, removed_ids: list, failed: int):
        # Update results list on the main thread
        removed_set = set(removed_ids)
        self._results = [r for r in self._results if r["id"] not in removed_set]
        self._checked -= removed_set

        self._set_scanning(False)
        self._refresh_table()
        self._status_var.set(
            f"Done — {len(removed_ids)} removed, {failed} failed."
        )
        if failed:
            messagebox.showwarning(
                "Partial Failure",
                f"{len(removed_ids)} members removed.\n"
                f"{failed} could not be removed (permission error or rate limit).",
            )
        else:
            messagebox.showinfo("Removal Complete",
                                f"{len(removed_ids)} inactive members removed.")

    # ── Export ────────────────────────────────────────────────────────────────

    def _export_csv(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            title="Save member report as CSV",
        )
        if not path:
            return

        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=["name", "id", "status", "admin"])
            writer.writeheader()
            for r in self._results:
                writer.writerow({
                    "name": r["name"],
                    "id": r["id"],
                    "status": "Active" if r["active"] else "Inactive",
                    "admin": "Yes" if r["admin"] else "No",
                })

        self._status_var.set(f"Exported {len(self._results)} members → {path}")
