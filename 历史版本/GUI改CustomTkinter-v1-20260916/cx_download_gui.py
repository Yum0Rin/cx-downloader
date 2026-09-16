#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""超星/学习通 文件批量下载器 —— 图形界面版

把预览页链接（或 objectid）粘进文本框，点「开始下载」即可。
底层复用同目录 cx_download.py 的接口逻辑。
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cx_download as core  # noqa: E402

SETTINGS = os.path.join(os.path.expanduser("~"), ".cx_download_gui.json")
DEFAULT_OUT = os.path.join(os.path.expanduser("~"), "Downloads", "超星下载")


def load_settings() -> dict:
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def save_settings(d: dict) -> None:
    try:
        with open(SETTINGS, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("超星 / 学习通 文件批量下载器")
        self.geometry("880x660")
        self.minsize(720, 560)

        cfg = load_settings()
        self.outdir = tk.StringVar(value=cfg.get("outdir") or DEFAULT_OUT)
        self.mode = tk.StringVar(value=cfg.get("mode") or "orig")
        self.jobs = tk.IntVar(value=int(cfg.get("jobs") or 4))
        self.overwrite = tk.BooleanVar(value=bool(cfg.get("overwrite")))
        self.cookie = tk.StringVar(value=cfg.get("cookie") or "")

        self.q: queue.Queue = queue.Queue()
        self.stop_evt = threading.Event()
        self.worker: threading.Thread | None = None
        self.total = 0
        self.done = 0
        self.ok = 0
        self.fail = 0
        self.t0 = 0.0

        self._build()
        self.after(100, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- 界面 ----------
    def _build(self):
        pad = {"padx": 10, "pady": 6}

        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        ttk.Label(
            top,
            text="把学习通预览页链接粘到下面（每行一个；也可以直接粘整段带链接的文字，会自动识别）",
            foreground="#2F4858",
        ).pack(anchor="w")

        box = ttk.Frame(self)
        box.pack(fill="both", expand=False, **pad)
        self.txt_in = tk.Text(box, height=8, wrap="none", font=("Consolas", 10))
        ysb = ttk.Scrollbar(box, orient="vertical", command=self.txt_in.yview)
        xsb = ttk.Scrollbar(box, orient="horizontal", command=self.txt_in.xview)
        self.txt_in.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        self.txt_in.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        box.columnconfigure(0, weight=1)
        self.txt_in.bind("<Control-Return>", lambda e: self.start())

        btns = ttk.Frame(box)
        btns.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(btns, text="从剪贴板粘贴", command=self.paste_clip).pack(side="left")
        ttk.Button(btns, text="清空", command=lambda: self.txt_in.delete("1.0", "end")).pack(
            side="left", padx=6
        )
        ttk.Button(btns, text="从文件导入…", command=self.import_file).pack(side="left")

        # 保存目录
        out = ttk.Frame(self)
        out.pack(fill="x", **pad)
        ttk.Label(out, text="保存目录").pack(side="left")
        ttk.Entry(out, textvariable=self.outdir).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(out, text="选择…", command=self.choose_dir).pack(side="left")
        ttk.Button(out, text="打开", command=self.open_dir).pack(side="left", padx=4)

        # 选项
        opt = ttk.LabelFrame(self, text="选项")
        opt.pack(fill="x", **pad)
        ttk.Radiobutton(opt, text="仅原件", variable=self.mode, value="orig").grid(
            row=0, column=0, padx=8, pady=4, sticky="w"
        )
        ttk.Radiobutton(opt, text="原件 + PDF", variable=self.mode, value="both").grid(
            row=0, column=1, padx=8, pady=4, sticky="w"
        )
        ttk.Radiobutton(opt, text="仅 PDF", variable=self.mode, value="pdf").grid(
            row=0, column=2, padx=8, pady=4, sticky="w"
        )
        ttk.Label(opt, text="并发").grid(row=0, column=3, padx=(20, 2), sticky="e")
        ttk.Spinbox(opt, from_=1, to=16, width=4, textvariable=self.jobs).grid(
            row=0, column=4, sticky="w"
        )
        ttk.Checkbutton(opt, text="强制覆盖已存在文件", variable=self.overwrite).grid(
            row=1, column=0, columnspan=3, padx=8, pady=4, sticky="w"
        )
        ttk.Label(opt, text="Cookie（一般留空）").grid(row=1, column=3, padx=(20, 2), sticky="e")
        ttk.Entry(opt, textvariable=self.cookie, width=26).grid(row=1, column=4, sticky="w")

        # 操作
        act = ttk.Frame(self)
        act.pack(fill="x", **pad)
        self.btn_start = ttk.Button(act, text="开始下载", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(act, text="停止", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        self.pb = ttk.Progressbar(act, mode="determinate")
        self.pb.pack(side="left", fill="x", expand=True, padx=10)
        self.lbl = ttk.Label(act, text="就绪", width=18, anchor="e")
        self.lbl.pack(side="left")

        # 日志
        lg = ttk.LabelFrame(self, text="日志")
        lg.pack(fill="both", expand=True, **pad)
        self.txt_log = tk.Text(lg, height=12, wrap="word", state="disabled", font=("Consolas", 10))
        lysb = ttk.Scrollbar(lg, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=lysb.set)
        self.txt_log.pack(side="left", fill="both", expand=True)
        lysb.pack(side="right", fill="y")
        self.txt_log.tag_configure("ok", foreground="#1a7f37")
        self.txt_log.tag_configure("err", foreground="#C03A2B")
        self.txt_log.tag_configure("dim", foreground="#777777")

    # ---------- 小工具 ----------
    def log(self, msg: str, tag: str | None = None):
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", msg + "\n", tag or "")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def paste_clip(self):
        try:
            self.txt_in.insert("end", self.clipboard_get() + "\n")
        except tk.TclError:
            messagebox.showinfo("提示", "剪贴板里没有文本")

    def import_file(self):
        p = filedialog.askopenfilename(
            title="选择链接文件", filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")]
        )
        if p:
            with open(p, encoding="utf-8-sig", errors="replace") as f:
                self.txt_in.insert("end", f.read().rstrip() + "\n")

    def choose_dir(self):
        p = filedialog.askdirectory(title="选择保存目录", initialdir=self.outdir.get() or None)
        if p:
            self.outdir.set(p)

    def open_dir(self):
        d = self.outdir.get()
        os.makedirs(d, exist_ok=True)
        os.startfile(d)  # noqa: S606

    # ---------- 下载 ----------
    def start(self):
        if self.worker and self.worker.is_alive():
            return
        raw = self.txt_in.get("1.0", "end")
        oids = core.extract_objectids(raw)
        if not oids:
            messagebox.showwarning("没找到链接", "请在文本框里粘贴预览页链接或 32 位 objectid。")
            return

        outdir = os.path.abspath(self.outdir.get())
        os.makedirs(outdir, exist_ok=True)
        mode = self.mode.get()
        want_original = mode in ("orig", "both")
        want_pdf = mode in ("pdf", "both")

        save_settings(
            {
                "outdir": self.outdir.get(),
                "mode": mode,
                "jobs": int(self.jobs.get()),
                "overwrite": bool(self.overwrite.get()),
                "cookie": self.cookie.get(),
            }
        )

        self.stop_evt.clear()
        self.total = len(oids)
        self.done = self.ok = self.fail = 0
        self.t0 = time.time()
        self.pb.configure(maximum=self.total, value=0)
        self.lbl.configure(text=f"0 / {self.total}")
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.log(f"共 {len(oids)} 个文件 → {outdir}", "dim")
        self.log(
            f"模式: {'仅PDF' if mode == 'pdf' else ('原件+PDF' if mode == 'both' else '仅原件')}",
            "dim",
        )

        self.worker = threading.Thread(
            target=self._run,
            args=(
                oids,
                outdir,
                want_original,
                want_pdf,
                self.cookie.get().strip() or None,
                max(1, int(self.jobs.get())),
                bool(self.overwrite.get()),
            ),
            daemon=True,
        )
        self.worker.start()

    def _run(self, oids, outdir, want_original, want_pdf, cookie, jobs, overwrite):
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            futs = {}
            for oid in oids:
                if self.stop_evt.is_set():
                    break
                futs[
                    ex.submit(
                        core.process_one,
                        oid,
                        outdir,
                        want_original,
                        want_pdf,
                        cookie,
                        self.timeout,
                        overwrite,
                    )
                ] = oid
            for fut in as_completed(futs):
                r = fut.result()
                self.q.put(("result", r))
        self.q.put(("finished", None))

    timeout = 60

    def stop(self):
        self.stop_evt.set()
        self.log("已请求停止，等待当前文件下载完成…", "dim")

    def _drain(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "result":
                    self._show(payload)
                elif kind == "finished":
                    self._finish()
        except queue.Empty:
            pass
        self.after(100, self._drain)

    def _show(self, r):
        self.done += 1
        self.pb.configure(value=self.done)
        self.lbl.configure(text=f"{self.done} / {self.total}")
        if r["ok"]:
            self.ok += 1
            self.log(f"[OK] {r.get('filename')}  ({r.get('pagenum')}p)", "ok")
            for dest, size, note in r["files"]:
                self.log(f"     {note}: {os.path.basename(dest)}  {core.human(size)}", "dim")
        else:
            self.fail += 1
            self.log(f"[失败] {r['objectid']}", "err")
            self.log(f"       {r['error']}", "err")

    def _finish(self):
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.lbl.configure(text=f"完成 {self.ok}/{self.total}")
        self.log(
            f"\n完成: 成功 {self.ok} / 失败 {self.fail}，耗时 {time.time() - self.t0:.1f}s",
            "dim",
        )
        self.log(f"目录: {os.path.abspath(self.outdir.get())}", "dim")

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askokcancel("正在下载", "还有任务在跑，确定要退出吗？"):
                return
            self.stop_evt.set()
        self.destroy()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
