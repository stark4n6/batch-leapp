#!/usr/bin/env python3
"""
batch_leapp_gui.py — a small Tkinter front-end for batch_leapp.

Pick an input directory of zips, an output directory, and a LEAPP tool (a .py
script OR a compiled binary / macOS .app), then run the whole batch with a live
log. Shares the exact engine the CLI uses (batch_leapp.run_batch).

    python batch_leapp_gui.py
"""

import os
import queue
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

import batch_leapp as core


DONE = "__DONE__"      # sentinel pushed on the queue when a run finishes

# leapps.org palette (mirrors css/global.css).
GOLD = "#F5C020"
GOLD_DK = "#D4A010"
OFF_BLACK = "#0E0E0E"
SURFACE = "#161616"
SURFACE2 = "#1C1C1C"
BORDER = "#2C2C2C"
TEXT = "#F0EDE6"
MUTED = "#888888"
OK_GREEN = "#A4C639"
FAIL_RED = "#E30613"

# High-resolution logo rendered at header size (no runtime upscaling).
GUI_LOGO_DATA_URI = "data:image/png;base64,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"


def detect_leapp():
    """Best-effort guess at an installed LEAPP tool to prefill the field."""
    import shutil
    for name in ("ileapp", "aleapp", "rleapp", "vleapp"):
        found = shutil.which(name)
        if found:
            return found
    if sys.platform == "darwin":
        for app in sorted(Path("/Applications").glob("*[lL][eE][aA][pP][pP]*.app")):
            if "gui" not in app.stem.lower():
                return str(app)
    return ""


def open_path(path):
    """Open a file/folder with the OS default handler."""
    path = str(path)
    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
    elif sys.platform == "win32":
        os.startfile(path)            # noqa: S606 (intended)
    else:
        subprocess.Popen(["xdg-open", path])


class BatchLeappGUI:
    def __init__(self, root):
        self.root = root
        root.title("Batch LEAPP")
        root.minsize(760, 560)

        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.last_index = None
        self.last_output = None

        self.input_dir = tk.StringVar()
        self.output_dir = tk.StringVar()
        self.leapp = tk.StringVar(value=detect_leapp())
        self.ftype = tk.StringVar(value="zip")
        self.jobs = tk.IntVar(value=1)
        self.skip_existing = tk.BooleanVar(value=False)
        self.dry_run = tk.BooleanVar(value=False)

        self._apply_theme()
        self._build()
        self.root.after(100, self._drain)

    # ---- theming ---------------------------------------------------------
    def _apply_theme(self):
        self.root.configure(bg=OFF_BLACK)
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background=OFF_BLACK, foreground=TEXT,
                        fieldbackground=SURFACE2, bordercolor=BORDER,
                        lightcolor=BORDER, darkcolor=BORDER, troughcolor=SURFACE)
        style.configure("TFrame", background=OFF_BLACK)
        style.configure("TLabel", background=OFF_BLACK, foreground=TEXT)
        style.configure("Header.TLabel", background=OFF_BLACK, foreground=TEXT,
                        font=("Helvetica Neue", 22, "bold"))
        style.configure("Status.TLabel", background=OFF_BLACK, foreground=MUTED)
        style.configure("TButton", background=SURFACE2, foreground=TEXT,
                        bordercolor=BORDER, focuscolor=GOLD, padding=6)
        style.map("TButton",
                  background=[("active", SURFACE), ("disabled", OFF_BLACK)],
                  foreground=[("disabled", MUTED)],
                  bordercolor=[("active", GOLD)])
        style.configure("Accent.TButton", background=GOLD, foreground=OFF_BLACK,
                        font=("Helvetica Neue", 12, "bold"), padding=6)
        style.map("Accent.TButton",
                  background=[("active", GOLD_DK), ("disabled", BORDER)],
                  foreground=[("disabled", MUTED)])
        style.configure("TEntry", fieldbackground=SURFACE2, foreground=TEXT,
                        insertcolor=GOLD, bordercolor=BORDER, padding=4)
        style.configure("TCheckbutton", background=OFF_BLACK, foreground=TEXT)
        style.map("TCheckbutton", background=[("active", OFF_BLACK)],
                  indicatorcolor=[("selected", GOLD)], foreground=[("active", GOLD)])
        style.configure("TSpinbox", fieldbackground=SURFACE2, foreground=TEXT,
                        arrowcolor=GOLD, bordercolor=BORDER, padding=3)

    def _load_logo(self):
        """Build a PhotoImage of the LEAPPs logo. Prefers GUI_LOGO_DATA_URI, a
        crisp copy pre-rendered at header size (drawn 1:1, no upscaling). Falls
        back to the index logo (subsampled) if needed. Kept on self for Tk GC."""
        try:
            b64 = GUI_LOGO_DATA_URI.split(",", 1)[1]
            self._logo_img = tk.PhotoImage(data=b64)
            return self._logo_img
        except Exception:
            pass
        try:
            b64 = core.LEAPP_LOGO_DATA_URI.split(",", 1)[1]
            img = tk.PhotoImage(data=b64)
            factor = max(1, img.height() // 56)
            if factor > 1:
                img = img.subsample(factor, factor)
            self._logo_img = img
            return img
        except Exception:
            return None

    # ---- layout ----------------------------------------------------------
    def _build(self):
        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(self.root, padding=12)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        header = ttk.Frame(frm)
        header.grid(row=0, column=0, columnspan=3, sticky="we", pady=(0, 12))
        logo = self._load_logo()
        if logo is not None:
            tk.Label(header, image=logo, bg=OFF_BLACK).pack(side="left", padx=(0, 12))
        titles = ttk.Frame(header)
        titles.pack(side="left", anchor="center")
        ttk.Label(titles, text="Batch LEAPP", style="Header.TLabel").pack(anchor="w")
        ttk.Label(titles, text="Run iLEAPP / ALEAPP / RLEAPP / VLEAPP across a "
                              "folder of zips", style="Status.TLabel").pack(anchor="w")

        self._path_row(frm, 1, "Input dir (zips)", self.input_dir, self._pick_indir)
        self._path_row(frm, 2, "Output dir", self.output_dir, self._pick_outdir)
        self._path_row(frm, 3, "LEAPP tool", self.leapp, self._pick_leapp)

        opts = ttk.Frame(frm)
        opts.grid(row=4, column=0, columnspan=3, sticky="we", **pad)
        ttk.Label(opts, text="Type").pack(side="left")
        ttk.Entry(opts, textvariable=self.ftype, width=7).pack(side="left", padx=(4, 16))
        ttk.Label(opts, text="Parallel jobs").pack(side="left")
        ttk.Spinbox(opts, from_=1, to=64, width=4, textvariable=self.jobs).pack(
            side="left", padx=(4, 16))
        ttk.Checkbutton(opts, text="Skip existing", variable=self.skip_existing).pack(
            side="left", padx=6)
        ttk.Checkbutton(opts, text="Dry run", variable=self.dry_run).pack(
            side="left", padx=6)

        btns = ttk.Frame(frm)
        btns.grid(row=5, column=0, columnspan=3, sticky="we", **pad)
        self.run_btn = ttk.Button(btns, text="Run", command=self._start,
                                  style="Accent.TButton")
        self.run_btn.pack(side="left")
        self.stop_btn = ttk.Button(btns, text="Stop", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        self.open_index_btn = ttk.Button(btns, text="Open report index",
                                         command=self._open_index, state="disabled")
        self.open_index_btn.pack(side="left", padx=6)
        self.open_out_btn = ttk.Button(btns, text="Open output folder",
                                       command=self._open_output, state="disabled")
        self.open_out_btn.pack(side="left", padx=6)
        self.status = ttk.Label(btns, text="Idle", style="Status.TLabel")
        self.status.pack(side="right")

        self.log = scrolledtext.ScrolledText(
            frm, height=18, wrap="none", font=("Menlo", 11),
            bg=SURFACE, fg=TEXT, insertbackground=GOLD, borderwidth=0,
            highlightthickness=1, highlightbackground=BORDER,
            selectbackground=GOLD_DK, selectforeground=OFF_BLACK)
        self.log.grid(row=6, column=0, columnspan=3, sticky="nsew", **pad)
        frm.rowconfigure(6, weight=1)
        self.log.tag_configure("ok", foreground=OK_GREEN)
        self.log.tag_configure("fail", foreground=FAIL_RED)
        self.log.tag_configure("muted", foreground=MUTED)
        self.log.tag_configure("accent", foreground=GOLD)
        self.log.configure(state="disabled")

    def _path_row(self, frm, row, label, var, cmd):
        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=4)
        ttk.Entry(frm, textvariable=var).grid(row=row, column=1, sticky="we", pady=4)
        ttk.Button(frm, text="Browse…", command=cmd).grid(row=row, column=2, padx=8)

    # ---- pickers ---------------------------------------------------------
    def _pick_indir(self):
        d = filedialog.askdirectory(title="Input directory of zips")
        if d:
            self.input_dir.set(d)

    def _pick_outdir(self):
        d = filedialog.askdirectory(title="Output directory for reports")
        if d:
            self.output_dir.set(d)

    def _pick_leapp(self):
        f = filedialog.askopenfilename(
            title="LEAPP script, binary, or .app",
            filetypes=[("LEAPP tool", "*.py *.exe *.app *"), ("All files", "*")])
        if not f:
            return
        if core.is_gui_build(Path(f)):
            messagebox.showerror(
                "GUI build selected",
                f"'{Path(f).name}' is the interactive GUI build and can't be used "
                f"for batch processing.\n\nChoose the command-line LEAPP tool "
                f"(e.g. ileapp.py or the CLI binary).")
            return
        self.leapp.set(f)

    # ---- run / stop ------------------------------------------------------
    def _start(self):
        if self.worker and self.worker.is_alive():
            return
        if not self.input_dir.get() or not self.output_dir.get():
            messagebox.showerror("Missing path", "Choose an input and output directory.")
            return
        if not self.dry_run.get() and not self.leapp.get():
            messagebox.showerror("Missing LEAPP tool",
                                 "Choose a LEAPP script, binary, or .app.")
            return
        if self.leapp.get() and core.is_gui_build(Path(self.leapp.get())):
            messagebox.showerror(
                "GUI build selected",
                f"'{Path(self.leapp.get()).name}' is the interactive GUI build and "
                f"can't be used for batch processing.\n\nChoose the command-line "
                f"LEAPP tool instead.")
            return

        self._clear_log()
        self.stop_event.clear()
        self.last_index = self.last_output = None
        self.run_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.open_index_btn.configure(state="disabled")
        self.open_out_btn.configure(state="disabled")
        self.status.configure(text="Running…")

        opts = dict(
            input_dir=self.input_dir.get(), output_dir=self.output_dir.get(),
            leapp=self.leapp.get() or "ileapp.py", type=self.ftype.get() or "zip",
            jobs=max(1, self.jobs.get()), skip_existing=self.skip_existing.get(),
            dry_run=self.dry_run.get(),
        )
        self.worker = threading.Thread(target=self._run_worker, args=(opts,), daemon=True)
        self.worker.start()

    def _run_worker(self, opts):
        try:
            result = core.run_batch(
                opts["input_dir"], opts["output_dir"], opts["leapp"],
                type=opts["type"], jobs=opts["jobs"],
                skip_existing=opts["skip_existing"], dry_run=opts["dry_run"],
                capture=True,                      # never spray to stdout
                log=lambda s: self.q.put(s),
                should_stop=self.stop_event.is_set,
            )
            self.q.put((DONE, result, None))
        except core.BatchError as ex:
            self.q.put((DONE, None, str(ex)))
        except Exception as ex:                    # surface unexpected errors
            self.q.put((DONE, None, f"Unexpected error: {ex}"))

    def _stop(self):
        self.stop_event.set()
        self.status.configure(text="Stopping…")
        self._append("\n[stop requested — finishing in-flight jobs]\n")

    # ---- queue pump (runs on the Tk main thread) -------------------------
    def _drain(self):
        try:
            while True:
                item = self.q.get_nowait()
                if isinstance(item, tuple) and item and item[0] == DONE:
                    self._finish(item[1], item[2])
                else:
                    self._append(item + "\n")
        except queue.Empty:
            pass
        self.root.after(100, self._drain)

    def _finish(self, result, error):
        self.run_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        if error:
            self.status.configure(text="Error")
            messagebox.showerror("Batch LEAPP", error)
            return
        self.last_index = result.get("index")
        self.last_output = Path(self.output_dir.get()).expanduser()
        n_fail = len(result["failed"])
        self.status.configure(
            text=f"Done — {len(result['ok'])} ok, {n_fail} failed, "
                 f"{len(result['skipped'])} skipped")
        if self.last_index:
            self.open_index_btn.configure(state="normal")
        if self.last_output and self.last_output.is_dir():
            self.open_out_btn.configure(state="normal")

    # ---- helpers ---------------------------------------------------------
    def _open_index(self):
        if self.last_index:
            webbrowser.open_new_tab(Path(self.last_index).as_uri())

    def _open_output(self):
        if self.last_output:
            open_path(self.last_output)

    @staticmethod
    def _line_tag(text):
        up = text.upper()
        if any(k in up for k in ("FAIL", "ERROR", "TIMEOUT")):
            return "fail"
        if " OK " in up or up.strip().startswith("OK"):
            return "ok"
        if (text.startswith("===") or "START" in up or "HEARTBEAT" in up
                or "still running" in text or "STILL RUNNING" in up):
            return "muted"
        return ""

    def _append(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text, self._line_tag(text))
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")


def main():
    root = tk.Tk()
    BatchLeappGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
