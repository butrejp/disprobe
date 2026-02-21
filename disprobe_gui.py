"""Lightweight Tkinter GUI for disprobe using ttkbootstrap (optional).

Usage: python gui_ttk.py

This wrapper does not modify `disprobe.py`. It runs it as a subprocess
with `--no-pause --json <tmpfile>` and renders the resulting JSON in a table.
"""
from __future__ import annotations

import json
import subprocess
import threading
import tempfile
import sys
import os
import shutil
from pathlib import Path
import tkinter as tk
from tkinter import ttk
import tkinter.font as tkfont
from tkinter import messagebox

DISPROBE = Path(__file__).resolve().parent / "disprobe.py"

try:
    import ttkbootstrap as tb
    from ttkbootstrap.constants import INFO, SUCCESS, DANGER
    USE_TTB = True
except Exception:
    USE_TTB = False


# Detect OS theme (dark/light) if `darkdetect` is available. Best-effort only.
IS_DARK = False
try:
    import darkdetect
    if hasattr(darkdetect, "isDark"):
        IS_DARK = bool(darkdetect.isDark())
    elif hasattr(darkdetect, "theme"):
        IS_DARK = str(darkdetect.theme()).lower().startswith("dark")
except Exception:
    IS_DARK = False


class SimpleScrollbar(tk.Canvas):
    """A lightweight scrollbar drawn on a Canvas so we can control colors.

    - Works as a drop-in replacement for the parts of Scrollbar used here:
      - Accepts `command` (e.g. widget.yview)
      - Exposes a `set(first, last)` method the widget calls with fractions
      - Calls `command('moveto', fraction)` when user drags/clicks
    """
    def __init__(self, master, orient='vertical', command=None, **kw):
        if orient not in ('vertical', 'horizontal'):
            raise ValueError('orient must be vertical or horizontal')
        self.orient = orient
        super().__init__(master, highlightthickness=0, bd=0, **kw)
        self.command = command
        self._first = 0.0
        self._last = 1.0
        self._drag = False
        self._drag_offset = 0
        self.bind('<Button-1>', self._on_click)
        self.bind('<B1-Motion>', self._on_drag)
        self.bind('<ButtonRelease-1>', self._on_release)
        self.bind('<Configure>', lambda e: self._draw())

    def set(self, first, last):
        try:
            self._first = float(first)
            self._last = float(last)
        except Exception:
            return
        self._draw()

    def _draw(self):
        self.delete('all')
        w = self.winfo_width()
        h = self.winfo_height()
        if self.orient == 'vertical':
            trough_coords = (0, 0, w, h)
            self.create_rectangle(*trough_coords, fill=self['bg'], outline=self['bg'])
            fh = max(10, int((self._last - self._first) * h))
            y1 = int(self._first * h)
            y2 = y1 + fh
            self.create_rectangle(2, y1, w-2, y2, fill=self._get_thumb_color(), outline='')
        else:
            trough_coords = (0, 0, w, h)
            self.create_rectangle(*trough_coords, fill=self['bg'], outline=self['bg'])
            fw = max(10, int((self._last - self._first) * w))
            x1 = int(self._first * w)
            x2 = x1 + fw
            self.create_rectangle(x1, 2, x2, h-2, fill=self._get_thumb_color(), outline='')

    def _get_thumb_color(self):
        return getattr(self, '_thumb_color', '#888888')

    def _on_click(self, event):
        if self.orient == 'vertical':
            h = self.winfo_height()
            y = event.y
            frac = max(0.0, min(1.0, y / float(h)))
        else:
            w = self.winfo_width()
            x = event.x
            frac = max(0.0, min(1.0, x / float(w)))
        # move so clicked position becomes start
        if self.command:
            try:
                self.command('moveto', frac)
            except Exception:
                pass
        self._drag = True

    def _on_drag(self, event):
        if not self._drag:
            return
        if self.orient == 'vertical':
            h = self.winfo_height()
            y = event.y
            frac = max(0.0, min(1.0, y / float(h)))
        else:
            w = self.winfo_width()
            x = event.x
            frac = max(0.0, min(1.0, x / float(w)))
        if self.command:
            try:
                self.command('moveto', frac)
            except Exception:
                pass

    def _on_release(self, event):
        self._drag = False

    # compatibility alias
    def configure(self, **kw):
        # allow bg/thumb color options
        if 'thumbcolor' in kw:
            self._thumb_color = kw.pop('thumbcolor')
        try:
            super().configure(**kw)
        except Exception:
            # some callers use configure(bg=...)
            for k, v in kw.items():
                try:
                    self[k] = v
                except Exception:
                    pass

class DisprobeGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("disprobe GUI")

        self.frame = ttk.Frame(self.root, padding=8)
        self.frame.pack(fill="both", expand=True)

        self.top = ttk.Frame(self.frame)
        self.top.pack(fill="x")

        self.status_var = tk.StringVar(value="Idle")
        # record detected theme for use in theming
        self.is_dark = IS_DARK
        # placeholders; actual widgets created in the rss_row so they share one line
        self.status_lbl = None
        self.exit_lbl = None

        self.run_btn = ttk.Button(self.top, text="Run disprobe", command=self.start)
        # Run button will be placed in the bottom-right; do not pack here.
        # Settings button created but moved to bottom next to Open config
        self.settings_btn = ttk.Button(self.top, text="Settings", command=self.open_settings)

        # Determinate progress bars with labels
        # RSS row: label on the left, status/exit on the right (same line)
        self.rss_row = ttk.Frame(self.frame)
        self.rss_row.pack(fill="x")
        self.rss_label = ttk.Label(self.rss_row, text="Fetching RSS: 0/0")
        self.rss_label.pack(side="left", fill="x", expand=True)
        # create status frame at the right side of rss_row so status appears on same line
        try:
            status_frame = ttk.Frame(self.rss_row)
            status_frame.pack(side="right")
            # create fresh labels inside the status_frame
            self.status_lbl = ttk.Label(status_frame, textvariable=self.status_var)
            self.status_lbl.pack(side="left")
            self.exit_lbl = ttk.Label(status_frame, text="Exit: -")
            # do not pack exit_lbl yet; it will be shown when the run finishes
        except Exception:
            pass
        self.rss_prog = ttk.Progressbar(self.frame, mode="determinate", maximum=1)
        self.rss_prog.pack(fill="x", pady=(2, 0))

        self.pages_label = ttk.Label(self.frame, text="Fetching pages: 0/0")
        self.pages_label.pack(fill="x")
        self.pages_prog = ttk.Progressbar(self.frame, mode="determinate", maximum=1)
        self.pages_prog.pack(fill="x", pady=(2, 6))

        # Table with monospace font, scrollbars, and alternating row colors
        # Insert narrow separator columns between main columns to show vertical separators
        cols = ("distro", "sep1", "local", "sep2", "latest", "sep3", "status", "sep4", "source")
        self.tree_frame = ttk.Frame(self.frame)
        self.tree_frame.pack(fill="both", expand=True, pady=(8, 0))

        # Use OS default fonts; keep a slightly larger row height for readability
        style = ttk.Style()
        try:
            style.configure("Treeview", rowheight=20)
        except Exception:
            pass

        # hide built-in Treeview headings; we draw our own header above the tree
        self.tree = ttk.Treeview(self.tree_frame, columns=cols, show="", height=18)
        for c in cols:
            if c.startswith("sep"):
                # separator column: narrow and centered; heading left empty
                self.tree.heading(c, text="")
                # increase width to ensure visibility across themes
                self.tree.column(c, width=12, minwidth=8, anchor="center", stretch=False)
                continue
            # tuned widths for readability
            w = {
                "distro": 180,
                "local": 120,
                "latest": 120,
                "status": 140,
                "source": 220,
            }.get(c, 120)
            self.tree.heading(c, text=c.capitalize())
            self.tree.column(c, width=w, anchor="w", stretch=True)

        # Scrollbars
        # Use a custom Canvas scrollbar so colors can be controlled reliably
        self.v_scroll = SimpleScrollbar(self.tree_frame, orient="vertical", command=self.tree.yview, bg="#f0f0f0")
        # no horizontal scrollbar: tree will auto-wrap / columns sized to avoid horizontal scrolling
        self.tree.configure(yscrollcommand=self.v_scroll.set, xscrollcommand=lambda *a: None)
        # create a header canvas above the Treeview to render column headers and separators
        self.header_canvas = tk.Canvas(self.tree_frame, height=26, highlightthickness=0, bd=0)
        try:
            hbg = self.tree.cget("background")
            self.header_canvas.configure(bg=hbg)
        except Exception:
            pass
        self.header_canvas.pack(fill="x")

        self.tree.pack(side="left", fill="both", expand=True)
        self.v_scroll.pack(side="right", fill="y")

        # (Previous canvas-based separator overlay removed)

        # alternating row colors for readability and matching foreground
        if IS_DARK:
            odd_bg = "#252525"
            even_bg = "#202020"
            fg = "#ffffff"
        else:
            odd_bg = "#ffffff"
            even_bg = "#f7f7f7"
            fg = "#000000"
        try:
            # assign to instance so header drawing can use it
            self.fg = fg
            # force Treeview text color to match the chosen fg (helps ttkbootstrap themes)
            try:
                style.configure("Treeview", foreground=self.fg)
                style.configure("Treeview.Heading", foreground=self.fg)
                # ensure heading uses same color in active/pressed states
                style.map("Treeview.Heading", foreground=[("active", self.fg), ("!disabled", self.fg)])
            except Exception:
                pass
            # odd/even tags only control background so status tags can set foreground
            self.tree.tag_configure("odd", background=odd_bg)
            self.tree.tag_configure("even", background=even_bg)
        except Exception:
            pass

        # draw a custom header row on the header_canvas so separators and headers
        # render consistently across themes
        self.header_canvas = getattr(self, 'header_canvas', None)
        def _draw_header(event=None):
            try:
                # we will draw left-justified header text and separator lines
                if not self.header_canvas:
                    return
                self.header_canvas.delete("all")
                cols = self.tree['columns']
                x = 0
                h = int(self.header_canvas.winfo_height() or 26)
                pad = 6
                for i, c in enumerate(cols):
                    w = int(self.tree.column(c, option='width') or 0)
                    if c.startswith('sep'):
                        xpos = x + (w // 2)
                        self.header_canvas.create_line(xpos, 4, xpos, h-4, fill=self.fg, width=1)
                    else:
                        # left-justify text inside column
                        xpos = x + pad
                        text = c.capitalize()
                        self.header_canvas.create_text(xpos, h//2, text=text, fill=self.fg, font=(None, 10, 'bold'), anchor='w')
                    x += w
            except Exception:
                pass

        # register bindings to redraw header
        self.tree.bind('<Configure>', _draw_header)
        self.root.bind('<Configure>', _draw_header)
        self.root.after(100, _draw_header)
        # apply initial theme and draw header immediately
        try:
            self.apply_theme()
        except Exception:
            pass
        try:
            _draw_header()
        except Exception:
            pass

        # Raw JSON view toggle + bottom controls
        self.bot = ttk.Frame(self.frame)
        self.bot.pack(fill="x", pady=(8, 0))
        self.raw_btn = ttk.Button(self.bot, text="Show Raw JSON", command=self.toggle_raw)
        self.raw_btn.pack(side="left")
        self.debug_btn = ttk.Button(self.bot, text="Show Debug", command=self.toggle_debug)
        self.debug_btn.pack(side="left", padx=(8,0))
        self.open_cfg_btn = ttk.Button(self.bot, text="Open distros.txt", command=self.open_config)
        self.open_cfg_btn.pack(side="left", padx=(8, 0))
        # Settings placed next to Open config
        self.settings_btn = ttk.Button(self.bot, text="Settings", command=self.open_settings)
        self.settings_btn.pack(side="left", padx=(8, 0))
        # Run button on bottom-right
        self.run_btn = ttk.Button(self.bot, text="Run disprobe", command=self.start)
        self.run_btn.pack(side="right")

        # status_frame was moved to the rss_row to appear on the same line
        # as the 'Fetching RSS' label; ensure exit_lbl exists as an attribute
        try:
            # if not already created at top, create here for safety
            if not getattr(self, 'exit_lbl', None):
                self.exit_lbl = ttk.Label(self.top, text="Exit: -")
        except Exception:
            pass

        self.raw = tk.Text(self.frame, height=12)
        self.raw.pack(fill="both", expand=False)
        self.raw.pack_forget()

        self.debug = tk.Text(self.frame, height=12)
        self.debug.pack(fill="both", expand=False)
        self.debug.pack_forget()

        self.tmp_json = Path(tempfile.gettempdir()) / "disprobe_results.json"
        # settings persistence: when frozen prefer exe location (use argv[0] to
        # find the original exe path when using --onefile); otherwise keep next
        # to the source `disprobe.py`.
        try:
            if getattr(sys, 'frozen', False):
                exe_dir = Path(sys.argv[0]).resolve().parent
            else:
                exe_dir = DISPROBE.parent
        except Exception:
            exe_dir = DISPROBE.parent
        self.settings_path = exe_dir / "gui_settings.json"
        self._load_settings()

    def toggle_raw(self):
        if self.raw.winfo_ismapped():
            self.raw.pack_forget()
            self.raw_btn.config(text="Show Raw JSON")
        else:
            # hide debug pane if visible
            try:
                if self.debug.winfo_ismapped():
                    self.debug.pack_forget()
                    try:
                        self.debug_btn.config(text="Show Debug")
                    except Exception:
                        pass
            except Exception:
                pass
            self.raw.pack(fill="both", expand=False)
            self.raw_btn.config(text="Hide Raw JSON")

    def toggle_debug(self):
        if self.debug.winfo_ismapped():
            self.debug.pack_forget()
            self.debug_btn.config(text="Show Debug")
        else:
            # hide raw pane if visible
            try:
                if self.raw.winfo_ismapped():
                    self.raw.pack_forget()
                    try:
                        self.raw_btn.config(text="Show Raw JSON")
                    except Exception:
                        pass
            except Exception:
                pass
            self.debug.pack(fill="both", expand=False)
            self.debug_btn.config(text="Hide Debug")

    def start(self):
        self.run_btn.config(state="disabled")
        self.open_cfg_btn.config(state="disabled")
        # hide exit label until run finishes
        try:
            if self.exit_lbl.winfo_ismapped():
                self.exit_lbl.pack_forget()
        except Exception:
            pass
        # reset determinate progress bars
        try:
            self.rss_prog.config(maximum=1)
            self.rss_prog['value'] = 0
        except Exception:
            pass
        try:
            self.pages_prog.config(maximum=1)
            self.pages_prog['value'] = 0
        except Exception:
            pass
        self.status_var.set("Running...")
        # clear table
        for r in self.tree.get_children():
            self.tree.delete(r)
        self.raw.delete("1.0", "end")

        t = threading.Thread(target=self._run_subprocess, daemon=True)
        t.start()

    def _run_subprocess(self):
        # remove stale json
        try:
            if self.tmp_json.exists():
                self.tmp_json.unlink()
        except Exception:
            pass

        # When GUI is frozen (PyInstaller exe), avoid using sys.executable
        # to launch `disprobe.py` because that would re-run this same exe.
        # Prefer a sibling `disprobe.exe` (Windows) or the system `python`.
        try:
            if getattr(sys, 'frozen', False):
                exe_dir = Path(sys.executable).resolve().parent
                # prefer a sibling native executable if present
                if os.name == 'nt':
                    candidate = exe_dir / 'disprobe.exe'
                else:
                    candidate = exe_dir / 'disprobe'
                if candidate.exists():
                    args = [str(candidate), "--no-pause", "--json", str(self.tmp_json)]
                else:
                    # try to find a python interpreter on PATH
                    # prefer pythonw on Windows to avoid spawning a console
                    if os.name == 'nt':
                        py = shutil.which('pythonw') or shutil.which('python') or shutil.which('python3')
                    else:
                        py = shutil.which('python') or shutil.which('python3')
                    if py:
                        args = [py, str(DISPROBE), "--no-pause", "--json", str(self.tmp_json)]
                    else:
                        # last resort: fall back to sys.executable (may spawn another GUI)
                        args = [sys.executable, str(DISPROBE), "--no-pause", "--json", str(self.tmp_json)]
            else:
                args = [sys.executable, str(DISPROBE), "--no-pause", "--json", str(self.tmp_json)]
        except Exception:
            args = [sys.executable, str(DISPROBE), "--no-pause", "--json", str(self.tmp_json)]
        # append flags from saved settings
        try:
            s = self.settings
        except Exception:
            s = {}
        # -s sleep ms
        try:
            sm = int(s.get("sleep_ms", 500))
            args.append(f"-s{sm}")
        except Exception:
            pass
        # -p parallel tabs
        try:
            p = int(s.get("parallel_tabs", 8))
            args.append(f"-p{p}")
        except Exception:
            pass
        # --timeout <ms>
        try:
            to = int(s.get("timeout_ms", 15000))
            args.extend(["--timeout", str(to)])
        except Exception:
            pass
        # --retry-delay <ms>
        try:
            rd = int(s.get("retry_delay_ms", 1000))
            args.extend(["--retry-delay", str(rd)])
        except Exception:
            pass
        # --retries <n>
        try:
            rt = int(s.get("retries", 2))
            args.extend(["--retries", str(rt)])
        except Exception:
            pass
        # --rss-concurrency <n>
        try:
            rc = int(s.get("rss_concurrency", 8))
            args.extend(["--rss-concurrency", str(rc)])
        except Exception:
            pass
        # --no-browser
        try:
            if bool(s.get("no_browser", False)):
                args.append("--no-browser")
        except Exception:
            pass
        # preserve debug flag if user wants debugging in GUI
        try:
            if bool(s.get("debug", False)):
                args.append("--debug")
        except Exception:
            pass

        # start subprocess and stream stderr to capture textual progress bars
        import re, io
        # On Windows, suppress console windows for subprocesses when possible
        popen_kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        if os.name == 'nt':
            try:
                popen_kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
            except Exception:
                pass
        proc = subprocess.Popen(args, **popen_kwargs)

        stderr_buf = ""

        def process_progress_line(line: str):
            # look for patterns like: "Fetching RSS: [###----] 3/10" or "Fetching pages: [...] 2/8"
            m = re.search(r"(Fetching RSS|Fetching pages)[: ].*?(\d+)/(\d+)", line)
            if not m:
                return
            kind = m.group(1)
            completed = int(m.group(2))
            total = int(m.group(3))

            def ui_update():
                try:
                    if kind == "Fetching RSS":
                        self.rss_prog.config(maximum=total)
                        self.rss_prog['value'] = completed
                        self.rss_label.config(text=f"Fetching RSS: {completed}/{total}")
                    else:
                        self.pages_prog.config(maximum=total)
                        self.pages_prog['value'] = completed
                        self.pages_label.config(text=f"Fetching pages: {completed}/{total}")
                except Exception:
                    pass

            self.root.after(1, ui_update)

        # read stderr char-by-char so we can catch \r-updates
        try:
            stderr = proc.stderr
            while True:
                ch = stderr.read(1)
                if ch == '' and proc.poll() is not None:
                    break
                if ch == '':
                    continue
                stderr_buf += ch
                # process on carriage return or newline
                if '\r' in stderr_buf or '\n' in stderr_buf:
                    parts = re.split(r"[\r\n]", stderr_buf)
                    for part in parts[:-1]:
                        process_progress_line(part)
                        # try to parse debug JSON lines emitted by disprobe.debug_log
                        try:
                            j = json.loads(part)
                        except Exception:
                            j = None
                        if isinstance(j, dict) and 'event' in j:
                            def _append_debug(line=part):
                                try:
                                    try:
                                        self.debug.insert('end', line + "\n")
                                        self.debug.see('end')
                                    except Exception:
                                        pass
                                except Exception:
                                    pass
                            self.root.after(1, _append_debug)
                    stderr_buf = parts[-1]
        except Exception:
            # streaming failed; fall back to waiting for process
            proc.wait()

        exit_code = proc.wait()

        # read JSON if present
        data = None
        if self.tmp_json.exists():
            try:
                data = json.loads(self.tmp_json.read_text(encoding="utf-8"))
            except Exception as e:
                # try to capture stdout/stderr
                try:
                    out = proc.stdout.read() if proc.stdout else ""
                except Exception:
                    out = ""
                try:
                    err = proc.stderr.read() if proc.stderr else ""
                except Exception:
                    err = stderr_buf
                data = {"error": f"failed to parse json: {e}", "stdout": out, "stderr": err}
        else:
            try:
                out = proc.stdout.read() if proc.stdout else ""
            except Exception:
                out = ""
            data = {"error": "no json output", "stdout": out, "stderr": stderr_buf}

        # if any remaining buffered stderr contains JSON debug lines, append them
        try:
            if stderr_buf:
                for line in re.split(r"[\r\n]+", stderr_buf):
                    if not line:
                        continue
                    try:
                        j = json.loads(line)
                    except Exception:
                        j = None
                    if isinstance(j, dict) and 'event' in j:
                        try:
                            self.debug.insert('end', line + "\n")
                            self.debug.see('end')
                        except Exception:
                            pass
        except Exception:
            pass

        # schedule UI update on main thread
        self.root.after(10, lambda: self._update_ui(exit_code, data))

    def _load_settings(self):
        defaults = {
            "sleep_ms": 500,
            "parallel_tabs": 8,
            "timeout_ms": 15000,
            "retry_delay_ms": 1000,
            "retries": 2,
            "rss_concurrency": 8,
            "no_browser": False,
            "debug": False,
        }
        # keep a canonical copy of defaults for Restore Defaults
        base_defaults = defaults.copy()
        try:
            if self.settings_path.exists():
                txt = self.settings_path.read_text(encoding="utf-8")
                loaded = json.loads(txt)
                defaults.update(loaded)
        except Exception:
            pass
        self._defaults = base_defaults
        self.settings = defaults

    def _save_settings(self):
        try:
            self.settings_path.write_text(json.dumps(self.settings, indent=2), encoding="utf-8")
        except Exception as e:
            try:
                messagebox.showerror("Save Settings", f"Failed to save settings: {e}")
            except Exception:
                pass

    def open_settings(self):
        # reload settings from disk so dialog reflects any external edits
        try:
            self._load_settings()
        except Exception:
            pass

        dlg = tk.Toplevel(self.root)
        dlg.title("Settings")
        dlg.transient(self.root)
        dlg.grab_set()
        pad = {'padx': 6, 'pady': 4}
        # use StringVars so we can programmatically update fields (Restore Defaults)
        entries_vars = {}
        def add_row(parent, row, label, key):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky='w', **pad)
            var = tk.StringVar(value=str(self.settings.get(key, '')))
            ent = ttk.Entry(parent, textvariable=var)
            ent.grid(row=row, column=1, sticky='we', **pad)
            entries_vars[key] = var

        frm = ttk.Frame(dlg, padding=8)
        frm.grid(row=0, column=0, sticky='nsew')
        dlg.columnconfigure(0, weight=1)
        frm.columnconfigure(1, weight=1)

        add_row(frm, 0, "Sleep (ms) (-s)", 'sleep_ms')
        add_row(frm, 1, "Parallel tabs (-p)", 'parallel_tabs')
        add_row(frm, 2, "Timeout (ms)", 'timeout_ms')
        add_row(frm, 3, "Retry delay (ms)", 'retry_delay_ms')
        add_row(frm, 4, "Retries", 'retries')
        add_row(frm, 5, "RSS concurrency", 'rss_concurrency')

        # checkbuttons
        no_browser_var = tk.BooleanVar(value=bool(self.settings.get('no_browser', False)))
        ttk.Checkbutton(frm, text='No browser (use RSS only)', variable=no_browser_var).grid(row=6, column=0, columnspan=2, sticky='w', **pad)
        debug_var = tk.BooleanVar(value=bool(self.settings.get('debug', False)))
        ttk.Checkbutton(frm, text='Enable debug (--debug)', variable=debug_var).grid(row=7, column=0, columnspan=2, sticky='w', **pad)

        # footer holds Restore (left) and Save/Cancel (right)
        footer = ttk.Frame(dlg)
        footer.grid(row=1, column=0, sticky='we', padx=8, pady=8)
        footer.columnconfigure(0, weight=1)
        footer.columnconfigure(1, weight=0)
        btn_fr = ttk.Frame(footer)
        btn_fr.grid(row=0, column=1, sticky='e')

        def on_save():
            # validate and store
            try:
                new = {}
                new['sleep_ms'] = int(entries_vars['sleep_ms'].get())
                new['parallel_tabs'] = int(entries_vars['parallel_tabs'].get())
                new['timeout_ms'] = int(entries_vars['timeout_ms'].get())
                new['retry_delay_ms'] = int(entries_vars['retry_delay_ms'].get())
                new['retries'] = int(entries_vars['retries'].get())
                new['rss_concurrency'] = int(entries_vars['rss_concurrency'].get())
                new['no_browser'] = bool(no_browser_var.get())
                new['debug'] = bool(debug_var.get())
            except Exception as e:
                messagebox.showerror("Invalid value", f"Please enter valid numeric values: {e}")
                return
            self.settings.update(new)
            self._save_settings()
            dlg.destroy()

        def on_cancel():
            dlg.destroy()

        ttk.Button(btn_fr, text='Save', command=on_save).pack(side='right', padx=(4,0))
        ttk.Button(btn_fr, text='Cancel', command=on_cancel).pack(side='right')

        def on_restore():
            # populate fields with canonical defaults (do not save)
            try:
                defs = getattr(self, '_defaults', {})
                entries_vars.get('sleep_ms', tk.StringVar()).set(str(defs.get('sleep_ms', '')))
                entries_vars.get('parallel_tabs', tk.StringVar()).set(str(defs.get('parallel_tabs', '')))
                entries_vars.get('timeout_ms', tk.StringVar()).set(str(defs.get('timeout_ms', '')))
                entries_vars.get('retry_delay_ms', tk.StringVar()).set(str(defs.get('retry_delay_ms', '')))
                entries_vars.get('retries', tk.StringVar()).set(str(defs.get('retries', '')))
                entries_vars.get('rss_concurrency', tk.StringVar()).set(str(defs.get('rss_concurrency', '')))
                no_browser_var.set(bool(defs.get('no_browser', False)))
                debug_var.set(bool(defs.get('debug', False)))
            except Exception:
                pass

        # place Restore Defaults at the left side of the footer
        ttk.Button(footer, text='Restore Defaults', command=on_restore).grid(row=0, column=0, sticky='w')

        # center dialog over parent window
        try:
            dlg.update_idletasks()
            rw = self.root.winfo_width()
            rh = self.root.winfo_height()
            rx = self.root.winfo_rootx()
            ry = self.root.winfo_rooty()
            dw = dlg.winfo_width()
            dh = dlg.winfo_height()
            x = rx + max(0, (rw - dw) // 2)
            y = ry + max(0, (rh - dh) // 2)
            dlg.geometry(f"+{x}+{y}")
            # bring to front and focus first input
            try:
                dlg.lift()
                dlg.focus_force()
                # focus the first entry widget (Sleep) via grid lookup
                try:
                    first_widgets = frm.grid_slaves(row=0, column=1)
                    if first_widgets:
                        try:
                            first_widgets[0].focus_set()
                        except Exception:
                            pass
                except Exception:
                    pass
            except Exception:
                pass
        except Exception:
            pass

    def apply_theme(self):
        # apply current theme colors to widgets
        try:
            if self.is_dark:
                self.fg = "#ffffff"
                odd_bg = "#252525"
                even_bg = "#202020"
            else:
                self.fg = "#000000"
                odd_bg = "#ffffff"
                even_bg = "#f7f7f7"
            style = ttk.Style()
            try:
                # set tree foreground and backgrounds
                bg = even_bg
                style.configure("Treeview", foreground=self.fg, background=bg, fieldbackground=bg)
                style.configure("Treeview.Heading", foreground=self.fg)
                style.map("Treeview.Heading", foreground=[("active", self.fg), ("!disabled", self.fg)])
            except Exception:
                pass
            try:
                # ensure row background tags are updated; status foreground tags updated below
                self.tree.tag_configure("odd", background=odd_bg)
                self.tree.tag_configure("even", background=even_bg)
                # status tags: choose colors appropriate for current theme
                if self.is_dark:
                    st_colors = {
                        "UP TO DATE": "#00FFFF",          # cyan
                        "UPDATE AVAILABLE": "#FFF59D",   # light yellow
                        "LOCAL AHEAD": "#FF77FF",        # light magenta
                        "UNKNOWN": self.fg,
                    }
                else:
                    st_colors = {
                        "UP TO DATE": "#008B8B",         # darker cyan
                        "UPDATE AVAILABLE": "#B28700",  # darker yellow/gold
                        "LOCAL AHEAD": "#9A00A8",       # darker magenta/purple
                        "UNKNOWN": self.fg,
                    }
                try:
                    self.tree.tag_configure("st_up_to_date", foreground=st_colors["UP TO DATE"])
                    self.tree.tag_configure("st_update_available", foreground=st_colors["UPDATE AVAILABLE"])
                    self.tree.tag_configure("st_local_ahead", foreground=st_colors["LOCAL AHEAD"])
                    self.tree.tag_configure("st_unknown", foreground=st_colors["UNKNOWN"])
                except Exception:
                    pass
            except Exception:
                pass
            try:
                if hasattr(self, 'header_canvas') and self.header_canvas:
                    try:
                        self.header_canvas.configure(bg=bg)
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                # frame and general widget backgrounds
                try:
                    style.configure("App.TFrame", background=bg)
                    self.frame.configure(style="App.TFrame")
                    self.top.configure(style="App.TFrame")
                    self.bot.configure(style="App.TFrame")
                    self.tree_frame.configure(style="App.TFrame")
                except Exception:
                    try:
                        self.frame.configure(background=bg)
                        self.top.configure(background=bg)
                        self.bot.configure(background=bg)
                        self.tree_frame.configure(background=bg)
                    except Exception:
                        pass
                try:
                    style.configure("TLabel", background=bg, foreground=self.fg)
                    style.configure("TButton", background=bg, foreground=self.fg)
                except Exception:
                    try:
                        self.status_lbl.configure(background=bg, foreground=self.fg)
                        self.run_btn.configure(background=bg, foreground=self.fg)
                        self.open_cfg_btn.configure(background=bg, foreground=self.fg)
                        try:
                            self.settings_btn.configure(background=bg, foreground=self.fg)
                        except Exception:
                            pass
                        self.raw_btn.configure(background=bg, foreground=self.fg)
                    except Exception:
                        pass
                # button hover / active mapping
                try:
                    hover_bg = "#3a3a3a" if self.is_dark else "#e6e6e6"
                    hover_fg = self.fg
                    style.map('TButton', background=[('active', hover_bg), ('!disabled', bg)], foreground=[('active', hover_fg), ('!disabled', self.fg)])
                except Exception:
                    pass
                # progressbar and scrollbar styling
                try:
                    style.configure('TProgressbar', troughcolor=bg, background=self.fg)
                    style.configure('Horizontal.TProgressbar', troughcolor=bg, background=self.fg)
                except Exception:
                    pass
                try:
                    style.configure('Vertical.TScrollbar', troughcolor=bg, background=bg)
                    style.configure('Horizontal.TScrollbar', troughcolor=bg, background=bg)
                    # attempt to style ttk scrollbars; if we replaced with tk.Scrollbar,
                    # configure the widget colors directly below
                    try:
                        if hasattr(self, 'v_scroll') and self.v_scroll and isinstance(self.v_scroll, ttk.Scrollbar):
                            self.v_scroll.configure(style='Vertical.TScrollbar')
                        if hasattr(self, 'h_scroll') and self.h_scroll and isinstance(self.h_scroll, ttk.Scrollbar):
                            self.h_scroll.configure(style='Horizontal.TScrollbar')
                    except Exception:
                        pass
                except Exception:
                    # fallback: configure individual scrollbar widgets directly (tk-based SimpleScrollbar)
                    try:
                        if hasattr(self, 'v_scroll') and self.v_scroll:
                            try:
                                # set bg and thumb color for our SimpleScrollbar
                                thumb = "#555555" if self.is_dark else "#909090"
                                try:
                                    self.v_scroll.configure(bg=bg, thumbcolor=thumb, activebackground=hover_bg, highlightbackground=bg, highlightcolor=bg)
                                except Exception:
                                    try:
                                        self.v_scroll.configure(bg=bg)
                                    except Exception:
                                        pass
                                try:
                                    self.v_scroll._thumb_color = thumb
                                    self.v_scroll._draw()
                                except Exception:
                                    pass
                            except Exception:
                                try:
                                    self.v_scroll.configure(bg=bg)
                                except Exception:
                                    pass
                        # horizontal scrollbar intentionally omitted; ignore if present
                        if hasattr(self, 'h_scroll') and self.h_scroll:
                            try:
                                self.h_scroll.configure(bg=bg)
                            except Exception:
                                pass
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                # progressbar/trough colors (best-effort)
                try:
                    style.configure('TProgressbar', troughcolor=bg, background=self.fg)
                except Exception:
                    try:
                        style.configure('Horizontal.TProgressbar', troughcolor=bg, background=self.fg)
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                # raw text view colors
                try:
                    self.raw.configure(bg=bg, fg=self.fg)
                except Exception:
                    pass
            except Exception:
                pass
        except Exception:
            pass

    # theme toggle removed; theming is applied automatically at startup

    def open_config(self):
        cfg = DISPROBE.parent / "distros.txt"
        try:
            # Windows
            import os

            if hasattr(os, "startfile"):
                os.startfile(cfg)
                return
        except Exception:
            pass
        # macOS / Linux fallback
        try:
            if sys.platform == "darwin":
                subprocess.run(["open", str(cfg)])
            else:
                subprocess.run(["xdg-open", str(cfg)])
        except Exception:
            # last resort: open with notepad (Windows) or print path
            try:
                if sys.platform.startswith("win"):
                    subprocess.run(["notepad", str(cfg)])
                else:
                    print(f"Please open: {cfg}")
            except Exception:
                print(f"Please open: {cfg}")

    def _update_ui(self, exit_code: int, data: dict):
        # show exit label now that run finished (next to status)
        try:
            if not self.exit_lbl.winfo_ismapped():
                self.exit_lbl.pack(side="left", padx=(8, 0))
        except Exception:
            pass
        try:
            self.exit_lbl.config(text=f"Exit: {exit_code}")
        except Exception:
            pass
        self.status_var.set("Done")
        self.run_btn.config(state="normal")
        try:
            self.open_cfg_btn.config(state="normal")
        except Exception:
            pass

        # populate tree with alternating row backgrounds for clarity
        results = data.get("results") or []
        for idx, row in enumerate(results):
            d = row.get("distro") if isinstance(row, dict) else (row[0] if len(row) > 0 else "")
            if isinstance(row, dict):
                lv = row.get("local_version", "")
                dv = row.get("latest_version", "")
                st = row.get("status", "")
                src = row.get("source", "")
            else:
                # legacy tuple-style
                lv = row[1] if len(row) > 1 else ""
                dv = row[2] if len(row) > 2 else ""
                st = row[3] if len(row) > 3 else ""
                src = row[5] if len(row) > 5 else ""
            row_bg_tag = "odd" if (idx % 2) == 0 else "even"
            # map status string to a status tag (use uppercase keys as produced by disprobe)
            st_key = (st or "").upper()
            if st_key == "UP TO DATE":
                st_tag = "st_up_to_date"
            elif st_key == "UPDATE AVAILABLE":
                st_tag = "st_update_available"
            elif st_key == "LOCAL AHEAD":
                st_tag = "st_local_ahead"
            else:
                st_tag = "st_unknown"

            vals = (d, "|", lv, "|", dv, "|", st, "|", src)
            try:
                self.tree.insert("", "end", values=vals, tags=(row_bg_tag, st_tag))
            except Exception:
                # fallback to single tag
                try:
                    self.tree.insert("", "end", values=vals, tags=(row_bg_tag,))
                except Exception:
                    self.tree.insert("", "end", values=vals)

        # show raw
        self.raw.delete("1.0", "end")
        try:
            pretty = json.dumps(data, indent=2)
        except Exception:
            pretty = str(data)
        self.raw.insert("1.0", pretty)


def main():
    if USE_TTB:
        try:
            # Try to prefer a dark ttkbootstrap theme when the OS is in dark mode.
            if IS_DARK:
                try:
                    app = tb.Window(themename="darkly", title="disprobe GUI")
                except Exception:
                    try:
                        app = tb.Window(theme="darkly", title="disprobe GUI")
                    except Exception:
                        app = tb.Window(title="disprobe GUI")
            else:
                app = tb.Window(title="disprobe GUI")
            root = app
        except Exception:
            root = tk.Tk()
    else:
        root = tk.Tk()
        # Apply a darker palette when dark mode detected (best-effort).
        if IS_DARK:
            try:
                root.tk_setPalette(background="#2e2e2e", foreground="#ffffff")
            except Exception:
                pass

    gui = DisprobeGUI(root)
    root.geometry("850x700")
    root.mainloop()


if __name__ == "__main__":
    main()
