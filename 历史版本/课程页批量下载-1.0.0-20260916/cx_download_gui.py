#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""超星/学习通 文件批量下载器 —— CustomTkinter 图形界面版

把预览页链接（或 objectid）粘进文本框，点「开始下载」即可。
底层复用同目录 cx_download.py 的接口逻辑，命令行版不受影响。

依赖: customtkinter (pip install customtkinter)
"""

from __future__ import annotations

import ctypes
import json
import os
import queue
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from tkinter import filedialog, messagebox

import customtkinter as ctk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cx_download as core  # noqa: E402

SETTINGS = os.path.join(os.path.expanduser("~"), ".cx_download_gui.json")
DEFAULT_OUT = os.path.join(os.path.expanduser("~"), "Downloads", "超星下载")

# ---------------------------------------------------------------- 配色
# 点石国风五色（朱砂 / 黛蓝 / 石青 / 米白 / 墨）做成明暗两套，
# 用 pair() 拼成 CustomTkinter 的 (light, dark) 元组。
LIGHT = {
    "page": "#E9EDF2",
    "card": "#FFFFFF",
    "border": "#DFE5EC",
    "input": "#F1F4F8",
    "input_border": "#D3DBE4",
    "text": "#2F4858",
    "muted": "#5F6E7E",
    "primary": "#C03A2B",
    "primary_hover": "#A62F22",
    "secondary": "#EDF1F6",
    "secondary_hover": "#DDE4EC",
    "track": "#DFE6EE",
    "caption": "#2F4858",
    "caption_text": "#FFFFFF",
    "dark_titlebar": 0,
}
DARK = {
    "page": "#16191E",
    "card": "#20252C",
    "border": "#2E353E",
    "input": "#1A1E24",
    "input_border": "#333C46",
    "text": "#A8C6D8",
    "muted": "#8A97A5",
    "primary": "#D8503F",
    "primary_hover": "#E5675A",
    "secondary": "#2A313A",
    "secondary_hover": "#353E4A",
    "track": "#2E353E",
    "caption": "#20252C",
    "caption_text": "#D7DEE7",
    "dark_titlebar": 1,
}


def pair(key: str):
    """返回 CustomTkinter 需要的 (浅色, 深色) 颜色元组。"""
    return (LIGHT[key], DARK[key])


# 日志区配色。Tk 的 tag 颜色不支持 (浅,深) 元组，所以主题切换时手动重刷。
LOG_STYLE = {
    "light": {
        "bg": "#F4F7FA",
        "border": "#DCE3EB",
        "fg": "#2F4858",
        "tags": {
            "ok": "#1A7F37",
            "err": "#C03A2B",
            "dim": "#78838F",
            "head": "#2F6F86",
        },
    },
    "dark": {
        "bg": "#151A21",
        "border": "#2E353E",
        "fg": "#D7DEE7",
        "tags": {
            "ok": "#6FCF97",
            "err": "#EE7B6E",
            "dim": "#8A97A5",
            "head": "#9CC2E5",
        },
    },
}

FONT = "Microsoft YaHei UI"
MODE_LABELS = {"仅原件": "orig", "原件 + PDF": "both", "仅 PDF": "pdf"}
MODE_BY_VALUE = {v: k for k, v in MODE_LABELS.items()}
CONCURRENCY = ["1", "2", "4", "6", "8", "12", "16"]

# Win11 标题栏着色
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36
DWMWA_USE_IMMERSIVE_DARK_MODE = 20


def hex_to_colorref(hex_color: str) -> int:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return r | (g << 8) | (b << 16)


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


def est_width(text: str, font: ctk.CTkFont) -> int:
    """用真实字体度量算按钮宽度（measure 返回的是未缩放逻辑像素）。"""
    return max(58, font.measure(text) + 32)


class ToggleGroup(ctk.CTkFrame):
    """单选按钮组。

    用 CTkSegmentedButton 的问题是它只有**一个** text_color，选中态填饱和色时
    文字必然和底色撞车；而且它自己会在段间画分隔线。这里用一排 CTkButton
    自己实现，选中/未选中各自有独立的底色和文字色。
    """

    def __init__(self, master, values, variable, command=None, selected=None, **kw):
        super().__init__(master, fg_color="transparent", **kw)
        self.values = list(values)
        self.variable = variable
        self.command = command
        # selected = (fg, fg_hover, text)；默认用朱砂
        self.sel = selected or (pair("primary"), pair("primary_hover"), "#FFFFFF")
        self.buttons: dict[str, ctk.CTkButton] = {}
        self.font = ctk.CTkFont(FONT, 12)

        for i, v in enumerate(self.values):
            b = ctk.CTkButton(
                self,
                text=v,
                width=est_width(v, self.font),
                height=32,
                corner_radius=9,
                font=self.font,
                command=lambda v=v: self.set(v),
            )
            b.pack(side="left", padx=(0 if i == 0 else 6, 0))
            self.buttons[v] = b
        self._restyle()

    def _restyle(self):
        cur = self.variable.get()
        sel_fg, sel_hover, sel_text = self.sel
        for v, b in self.buttons.items():
            if v == cur:
                b.configure(fg_color=sel_fg, hover_color=sel_hover, text_color=sel_text)
            else:
                b.configure(
                    fg_color=pair("secondary"),
                    hover_color=pair("secondary_hover"),
                    text_color=pair("text"),
                )

    def set(self, value: str):
        if value not in self.buttons:
            return
        self.variable.set(value)
        self._restyle()
        if self.command:
            self.command(value)

    def get(self) -> str:
        return self.variable.get()


class Card(ctk.CTkFrame):
    """带小标题的卡片容器。"""

    def __init__(self, master, title: str, **kw):
        kw.setdefault("fg_color", pair("card"))
        kw.setdefault("corner_radius", 12)
        kw.setdefault("border_width", 1)
        kw.setdefault("border_color", pair("border"))
        super().__init__(master, **kw)
        ctk.CTkLabel(
            self,
            text=title,
            font=ctk.CTkFont(FONT, 12, "bold"),
            text_color=pair("primary"),
            anchor="w",
            height=20,
        ).pack(fill="x", padx=14, pady=(10, 0))


class App(ctk.CTk):
    timeout = 60

    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("blue")

        self.title("超星 / 学习通 文件批量下载器")
        self.configure(fg_color=pair("page"))
        self._fit_geometry(880, 680)
        self.minsize(720, 520)

        cfg = load_settings()
        self.outdir = ctk.StringVar(value=cfg.get("outdir") or DEFAULT_OUT)
        self.mode = ctk.StringVar(value=MODE_BY_VALUE.get(cfg.get("mode", "orig"), "仅原件"))
        self.jobs = ctk.StringVar(value=str(cfg.get("jobs") or 4))
        self.overwrite = ctk.BooleanVar(value=bool(cfg.get("overwrite")))
        self.cookie = ctk.StringVar(value=cfg.get("cookie") or "")
        self.appearance = ctk.StringVar(value=cfg.get("appearance") or "跟随系统")

        self.q: queue.Queue = queue.Queue()
        self.stop_evt = threading.Event()
        self.worker: threading.Thread | None = None
        self.total = self.done = self.ok = self.fail = 0
        self.t0 = 0.0
        self._last_mode = ""

        self._build()
        self._apply_appearance(self.appearance.get())
        self._restyle_for_theme()
        self.after(100, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- 窗口 ----------------
    def _fit_geometry(self, want_w: int, want_h: int):
        """按真实物理分辨率 + CTk 缩放换算，保证窗口一定放得下。"""
        try:
            sw = ctypes.windll.user32.GetSystemMetrics(0)
            sh = ctypes.windll.user32.GetSystemMetrics(1)
            scale = ctk.ScalingTracker.get_widget_scaling(self) or 1.0
            avail_w = int(sw / scale) - 40
            avail_h = int(sh / scale) - 70
            want_w = max(640, min(want_w, avail_w))
            want_h = max(480, min(want_h, avail_h))
        except Exception:  # noqa: BLE001
            pass
        self.geometry(f"{want_w}x{want_h}")

    def _apply_titlebar(self):
        """把系统标题栏染成与主题一致的颜色（Win11 22000+）。"""
        try:
            mode = ctk.get_appearance_mode().lower()
            dark = 1 if mode == "dark" else 0
            pal = DARK if dark else LIGHT
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            dwm = ctypes.windll.dwmapi
            for attr, color in (
                (DWMWA_USE_IMMERSIVE_DARK_MODE, dark),
                (DWMWA_CAPTION_COLOR, hex_to_colorref(pal["caption"])),
                (DWMWA_TEXT_COLOR, hex_to_colorref(pal["caption_text"])),
            ):
                v = ctypes.c_int(color)
                dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))
        except Exception:  # noqa: BLE001
            pass

    def _style_log(self):
        """日志框跟随主题：浅色用浅底深字，深色用控制台配色。"""
        mode = "dark" if ctk.get_appearance_mode().lower() == "dark" else "light"
        st = LOG_STYLE[mode]
        self.txt_log.configure(
            fg_color=st["bg"],
            text_color=st["fg"],
            border_color=st["border"],
        )
        for tag, color in st["tags"].items():
            self.txt_log.tag_config(tag, foreground=color)

    def _restyle_for_theme(self):
        self._apply_titlebar()
        self._style_log()
        # 选中态是靠变量算出来的，变量被外部改过时这里一并同步
        for tg in (self.tg_appearance, self.tg_mode):
            tg._restyle()

    # ---------------- 界面 ----------------
    def _build(self):
        self._build_header()

        wrap = ctk.CTkFrame(self, fg_color="transparent")
        wrap.pack(fill="both", expand=True, padx=14, pady=(2, 12))

        self._build_input(wrap)
        self._build_outdir(wrap)
        self._build_options(wrap)
        self._build_actions(wrap)
        self._build_log(wrap)

    def _build_header(self):
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(12, 4))

        left = ctk.CTkFrame(head, fg_color="transparent")
        left.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            left,
            text="超星文件下载器",
            font=ctk.CTkFont(FONT, 21, "bold"),
            text_color=pair("text"),
            anchor="w",
            height=28,
        ).pack(anchor="w")
        ctk.CTkLabel(
            left,
            text="粘贴学习通预览页链接或 objectid，自动抓取签名直链下载原件 / PDF",
            font=ctk.CTkFont(FONT, 12),
            text_color=pair("muted"),
            anchor="w",
            height=18,
        ).pack(anchor="w")

        self.tg_appearance = ToggleGroup(
            head,
            values=["跟随系统", "浅色", "深色"],
            variable=self.appearance,
            command=self._on_appearance,
            selected=(pair("text"), pair("secondary_hover"), ("#FFFFFF", "#16191E")),
        )
        self.tg_appearance.pack(side="right", padx=(10, 0))

    def _build_input(self, parent):
        card = Card(parent, "① 链接输入")
        card.pack(fill="x", pady=(6, 8))

        self.txt_in = ctk.CTkTextbox(
            card,
            height=104,
            corner_radius=9,
            border_width=1,
            border_color=pair("input_border"),
            fg_color=pair("input"),
            text_color=pair("text"),
            font=ctk.CTkFont("Consolas", 12),
            wrap="none",
        )
        self.txt_in.pack(fill="x", padx=14, pady=(8, 6))
        self.txt_in.bind("<Control-Return>", lambda e: self.start())
        self.txt_in.bind("<KeyRelease>", lambda e: self._count())

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(0, 12))
        self._btn(row, "从剪贴板粘贴", self.paste_clip, width=124).pack(side="left")
        self._btn(row, "从文件导入…", self.import_file, width=108).pack(side="left", padx=8)
        self._btn(row, "清空", self.clear_input, width=68).pack(side="left")
        self.lbl_count = ctk.CTkLabel(
            row, text="", font=ctk.CTkFont(FONT, 12), text_color=pair("muted"), height=32
        )
        self.lbl_count.pack(side="right")

    def _build_outdir(self, parent):
        card = Card(parent, "② 保存目录")
        card.pack(fill="x", pady=8)

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(8, 12))
        ctk.CTkEntry(
            row,
            textvariable=self.outdir,
            height=32,
            corner_radius=9,
            border_width=1,
            border_color=pair("input_border"),
            fg_color=pair("input"),
            text_color=pair("text"),
            font=ctk.CTkFont(FONT, 12),
        ).pack(side="left", fill="x", expand=True, padx=(0, 8))
        self._btn(row, "选择…", self.choose_dir, width=76).pack(side="left")
        self._btn(row, "打开", self.open_dir, width=66).pack(side="left", padx=8)

    def _build_options(self, parent):
        card = Card(parent, "③ 选项")
        card.pack(fill="x", pady=8)

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(8, 6))

        ctk.CTkLabel(
            row, text="下载内容", font=ctk.CTkFont(FONT, 12),
            text_color=pair("muted"), height=32,
        ).pack(side="left", padx=(0, 8))
        self.tg_mode = ToggleGroup(
            row,
            values=list(MODE_LABELS),
            variable=self.mode,
        )
        self.tg_mode.pack(side="left")

        ctk.CTkLabel(
            row, text="并发数", font=ctk.CTkFont(FONT, 12),
            text_color=pair("muted"), height=32,
        ).pack(side="left", padx=(22, 8))
        ctk.CTkOptionMenu(
            row,
            values=CONCURRENCY,
            variable=self.jobs,
            width=74,
            height=32,
            corner_radius=9,
            font=ctk.CTkFont(FONT, 12),
            fg_color=pair("secondary"),
            button_color=pair("secondary_hover"),
            button_hover_color=pair("border"),
            text_color=pair("text"),
            dropdown_fg_color=pair("card"),
            dropdown_hover_color=pair("secondary"),
            dropdown_text_color=pair("text"),
        ).pack(side="left")

        row2 = ctk.CTkFrame(card, fg_color="transparent")
        row2.pack(fill="x", padx=14, pady=(0, 12))
        ctk.CTkSwitch(
            row2,
            text="强制覆盖已存在文件",
            variable=self.overwrite,
            font=ctk.CTkFont(FONT, 12),
            text_color=pair("text"),
            progress_color=pair("primary"),
            fg_color=pair("input_border"),
            button_color=("#FFFFFF", "#E4EAF0"),
            button_hover_color=("#FFFFFF", "#FFFFFF"),
        ).pack(side="left")
        ctk.CTkLabel(
            row2, text="Cookie（一般留空）", font=ctk.CTkFont(FONT, 12),
            text_color=pair("muted"), height=32,
        ).pack(side="left", padx=(22, 8))
        ctk.CTkEntry(
            row2,
            textvariable=self.cookie,
            height=32,
            corner_radius=9,
            border_width=1,
            border_color=pair("input_border"),
            fg_color=pair("input"),
            text_color=pair("text"),
            placeholder_text="仅登录受限文件需要",
            placeholder_text_color=pair("muted"),
            font=ctk.CTkFont(FONT, 12),
        ).pack(side="left", fill="x", expand=True)

    def _build_actions(self, parent):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=(10, 8))

        self.btn_start = ctk.CTkButton(
            row,
            text="开始下载",
            width=124,
            height=40,
            corner_radius=11,
            font=ctk.CTkFont(FONT, 14, "bold"),
            fg_color=pair("primary"),
            hover_color=pair("primary_hover"),
            text_color="#FFFFFF",
            command=self.start,
        )
        self.btn_start.pack(side="left")

        self.btn_stop = ctk.CTkButton(
            row,
            text="停止",
            width=84,
            height=40,
            corner_radius=11,
            font=ctk.CTkFont(FONT, 13),
            fg_color=pair("secondary"),
            hover_color=pair("secondary_hover"),
            text_color=pair("text"),
            text_color_disabled=pair("muted"),
            state="disabled",
            command=self.stop,
        )
        self.btn_stop.pack(side="left", padx=10)

        self.pb = ctk.CTkProgressBar(
            row,
            height=8,
            corner_radius=4,
            fg_color=pair("track"),
            progress_color=pair("track"),
        )
        self.pb.set(0)
        self.pb.pack(side="left", fill="x", expand=True, padx=(6, 12))

        self.lbl = ctk.CTkLabel(
            row, text="就绪", width=120, anchor="e",
            font=ctk.CTkFont(FONT, 13), text_color=pair("muted"), height=40,
        )
        self.lbl.pack(side="left")

    def _build_log(self, parent):
        card = Card(parent, "④ 日志")
        card.pack(fill="both", expand=True, pady=(8, 0))

        self.txt_log = ctk.CTkTextbox(
            card,
            height=132,
            corner_radius=9,
            border_width=1,
            border_color=pair("border"),
            fg_color=pair("card"),
            text_color=pair("text"),
            font=ctk.CTkFont("Consolas", 12),
            wrap="word",
        )
        self.txt_log.pack(fill="both", expand=True, padx=14, pady=(8, 12))
        self.txt_log.configure(state="disabled")
        self._style_log()

    def _btn(self, master, text, command, width=100, **kw):
        kw.setdefault("height", 32)
        kw.setdefault("corner_radius", 9)
        kw.setdefault("font", ctk.CTkFont(FONT, 12))
        return ctk.CTkButton(
            master,
            text=text,
            width=width,
            command=command,
            fg_color=pair("secondary"),
            hover_color=pair("secondary_hover"),
            text_color=pair("text"),
            text_color_disabled=pair("muted"),
            **kw,
        )

    # ---------------- 交互 ----------------
    def _apply_appearance(self, choice: str):
        ctk.set_appearance_mode(
            {"跟随系统": "system", "浅色": "light", "深色": "dark"}.get(choice, "system")
        )

    def _on_appearance(self, choice):
        self._apply_appearance(choice)
        self.after(60, self._restyle_for_theme)

    def _count(self):
        n = len(core.extract_objectids(self.txt_in.get("1.0", "end")))
        self.lbl_count.configure(text=f"已识别 {n} 个" if n else "")

    def paste_clip(self):
        try:
            self.txt_in.insert("end", self.clipboard_get().rstrip() + "\n")
        except Exception:  # noqa: BLE001
            messagebox.showinfo("提示", "剪贴板里没有文本")
        self._count()

    def clear_input(self):
        self.txt_in.delete("1.0", "end")
        self._count()

    def import_file(self):
        p = filedialog.askopenfilename(
            title="选择链接文件", filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")]
        )
        if p:
            with open(p, encoding="utf-8-sig", errors="replace") as f:
                self.txt_in.insert("end", f.read().rstrip() + "\n")
            self._count()

    def choose_dir(self):
        p = filedialog.askdirectory(title="选择保存目录", initialdir=self.outdir.get() or None)
        if p:
            self.outdir.set(p)

    def open_dir(self):
        d = self.outdir.get()
        os.makedirs(d, exist_ok=True)
        os.startfile(d)  # noqa: S606

    def log(self, msg: str, tag: str = ""):
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", msg + "\n", tag)
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def _progress(self, value: float):
        """0 进度时把填充色也设成轨道色，避免左端残留一小点。"""
        self.pb.configure(progress_color=pair("primary") if value > 0 else pair("track"))
        self.pb.set(value)

    # ---------------- 下载 ----------------
    def start(self):
        if self.worker and self.worker.is_alive():
            return
        oids = core.extract_objectids(self.txt_in.get("1.0", "end"))
        if not oids:
            messagebox.showwarning("没找到链接", "请粘贴预览页链接或 32 位 objectid。")
            return

        outdir = os.path.abspath(self.outdir.get())
        os.makedirs(outdir, exist_ok=True)
        mode = MODE_LABELS.get(self.mode.get(), "orig")
        want_original = mode in ("orig", "both")
        want_pdf = mode in ("pdf", "both")
        jobs = max(1, int(self.jobs.get() or 4))
        overwrite = bool(self.overwrite.get())
        cookie = self.cookie.get().strip() or None

        save_settings(
            {
                "outdir": self.outdir.get(),
                "mode": mode,
                "jobs": jobs,
                "overwrite": overwrite,
                "cookie": self.cookie.get(),
                "appearance": self.appearance.get(),
            }
        )

        self.stop_evt.clear()
        self.total = len(oids)
        self.done = self.ok = self.fail = 0
        self.t0 = time.time()
        self._progress(0)
        self.lbl.configure(text=f"0 / {self.total}", text_color=pair("muted"))
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")

        self.log(f"共 {len(oids)} 个文件 → {outdir}", "head")
        self.log(f"模式: {self.mode.get()}　并发: {jobs}", "dim")

        self.worker = threading.Thread(
            target=self._run,
            args=(oids, outdir, want_original, want_pdf, cookie, jobs, overwrite),
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
                self.q.put(("result", fut.result()))
        self.q.put(("finished", None))

    def stop(self):
        self.stop_evt.set()
        self.log("已请求停止，等待当前文件完成…", "dim")

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

        # 跟随系统主题时，系统切换后重染标题栏
        mode = ctk.get_appearance_mode().lower()
        if mode != self._last_mode:
            self._last_mode = mode
            self._restyle_for_theme()

        self.after(100, self._drain)

    def _show(self, r):
        self.done += 1
        self._progress(self.done / max(1, self.total))
        self.lbl.configure(text=f"{self.done} / {self.total}")
        if r["ok"]:
            self.ok += 1
            self.log(f"✔ {r.get('filename')}　({r.get('pagenum')} 页)", "ok")
            for dest, size, note in r["files"]:
                self.log(f"    {note}: {os.path.basename(dest)}　{core.human(size)}", "dim")
        else:
            self.fail += 1
            self.log(f"✘ {r['objectid']}", "err")
            self.log(f"    {r['error']}", "err")

    def _finish(self):
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.lbl.configure(
            text=f"完成 {self.ok}/{self.total}",
            text_color=pair("primary") if self.fail else pair("text"),
        )
        self.log(
            f"\n完成: 成功 {self.ok} / 失败 {self.fail}，耗时 {time.time() - self.t0:.1f}s", "head"
        )
        self.log(f"目录: {os.path.abspath(self.outdir.get())}", "dim")

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askokcancel("正在下载", "还有任务在跑，确定要退出吗？"):
                return
            self.stop_evt.set()
        self.destroy()


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
