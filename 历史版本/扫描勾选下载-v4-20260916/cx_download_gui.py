#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""超星/学习通 文件批量下载器 —— CustomTkinter 图形界面版

流程：**粘贴 → 扫描 → 勾选 → 下载**

1. 把预览页链接 / objectid / 整个课程页链接粘进文本框；
2. 点「扫描」，课程页会把各章节的任务点附件全部列到下面的任务列表里，
   并检查本地是否已有同名文件、是不是同一份（按 字节数 + 前 64KB md5 判定）；
3. 列表默认全选，不需要的取消勾选；已存在的文件可以逐个点「跳过 / 覆盖」；
4. 点「开始下载」。

课程页扫描要用登录态：优先读本机 Firefox 的 cookies.sqlite 复用登录，
读不到就用手填的 Cookie（任何浏览器都能复制到）。

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
    "ok": "#1A7F37",
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
    "ok": "#6FCF97",
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
        self._fit_geometry(1120, 830)
        self.minsize(900, 600)

        cfg = load_settings()
        self.outdir = ctk.StringVar(value=cfg.get("outdir") or DEFAULT_OUT)
        self.mode = ctk.StringVar(value=MODE_BY_VALUE.get(cfg.get("mode", "orig"), "仅原件"))
        self.jobs = ctk.StringVar(value=str(cfg.get("jobs") or 4))
        self.auto_skip = ctk.BooleanVar(value=bool(cfg.get("auto_skip", True)))
        self.cookie = ctk.StringVar(value=cfg.get("cookie") or "")
        self.appearance = ctk.StringVar(value=cfg.get("appearance") or "跟随系统")

        self.q: queue.Queue = queue.Queue()
        self.stop_evt = threading.Event()
        self.worker: threading.Thread | None = None
        self.scan_worker: threading.Thread | None = None
        # 任务列表（扫描结果）。每个元素见 _add_task_row 里的字段说明。
        self.tasks: list[dict] = []
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

        # 左列：输入 / 目录 / 选项（高度固定）
        left = ctk.CTkFrame(wrap, fg_color="transparent", width=396)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        # 右列：任务列表（撑满）/ 日志
        right = ctk.CTkFrame(wrap, fg_color="transparent")
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))

        self._build_input(left)
        self._build_outdir(left)
        self._build_options(left)

        self._build_tasks(right)
        self._build_log(right)

        self._build_actions(self)

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
            text="预览页链接 / objectid / 整门课程页链接 —— 课程页可一键扫出全部任务点附件",
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
        card = Card(parent, "① 链接输入（预览页 / objectid / 课程页）")
        card.pack(fill="x", pady=(6, 8))

        self.txt_in = ctk.CTkTextbox(
            card,
            height=112,
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
        self.txt_in.bind("<KeyRelease>", lambda e: self._update_count())

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
        self.tg_mode = ToggleGroup(row, values=list(MODE_LABELS), variable=self.mode)
        self.tg_mode.pack(side="left")

        row2 = ctk.CTkFrame(card, fg_color="transparent")
        row2.pack(fill="x", padx=14, pady=(0, 6))
        ctk.CTkLabel(
            row2, text="并发数", font=ctk.CTkFont(FONT, 12),
            text_color=pair("muted"), height=32,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkOptionMenu(
            row2,
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
        ctk.CTkSwitch(
            row2,
            text="自动跳过相同文件",
            variable=self.auto_skip,
            command=self._update_sel,
            font=ctk.CTkFont(FONT, 12),
            text_color=pair("text"),
            progress_color=pair("primary"),
            fg_color=pair("input_border"),
            button_color=("#FFFFFF", "#E4EAF0"),
            button_hover_color=("#FFFFFF", "#FFFFFF"),
        ).pack(side="left", padx=(20, 0))

        row3 = ctk.CTkFrame(card, fg_color="transparent")
        row3.pack(fill="x", padx=14, pady=(0, 12))
        ctk.CTkLabel(
            row3, text="Cookie", font=ctk.CTkFont(FONT, 12),
            text_color=pair("muted"), height=32,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkEntry(
            row3,
            textvariable=self.cookie,
            height=32,
            corner_radius=9,
            border_width=1,
            border_color=pair("input_border"),
            fg_color=pair("input"),
            text_color=pair("text"),
            placeholder_text="一般留空；读不到 Firefox 登录态时再填",
            placeholder_text_color=pair("muted"),
            font=ctk.CTkFont(FONT, 12),
        ).pack(side="left", fill="x", expand=True)

    def _build_tasks(self, parent):
        card = Card(parent, "④ 任务列表（默认全选，勾掉不需要的）")
        card.pack(fill="both", expand=True, pady=8)

        bar = ctk.CTkFrame(card, fg_color="transparent")
        bar.pack(fill="x", padx=14, pady=(6, 4))
        self._btn(bar, "全选", lambda: self._set_all(True), width=62).pack(side="left")
        self._btn(bar, "全不选", lambda: self._set_all(False), width=72).pack(side="left", padx=6)
        self._btn(bar, "反选", self._invert_all, width=62).pack(side="left")
        self.lbl_sel = ctk.CTkLabel(
            bar, text="", font=ctk.CTkFont(FONT, 12), text_color=pair("muted"), height=32
        )
        self.lbl_sel.pack(side="right")

        self.list_box = ctk.CTkScrollableFrame(
            card,
            fg_color=pair("input"),
            corner_radius=9,
            border_width=1,
            border_color=pair("input_border"),
        )
        self.list_box.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        self.list_box.grid_columnconfigure(1, weight=1)
        self.empty_hint = None
        self._show_empty_hint()

    def _show_empty_hint(self):
        self.empty_hint = ctk.CTkLabel(
            self.list_box,
            text="还没有内容 —— 在上面粘好链接，点「扫描」把文件列出来",
            font=ctk.CTkFont(FONT, 12),
            text_color=pair("muted"),
        )
        self.empty_hint.grid(row=0, column=0, columnspan=4, pady=26)

    def _clear_tasks(self):
        for w in self.list_box.winfo_children():
            w.destroy()
        self.tasks = []
        self.empty_hint = None
        self._update_sel()

    def _render_tasks(self):
        """按 self.tasks 重建整个列表（行数不多，直接重建最省心）。"""
        for w in self.list_box.winfo_children():
            w.destroy()
        self.empty_hint = None
        if not self.tasks:
            self._show_empty_hint()
        else:
            for i, t in enumerate(self.tasks):
                self._add_task_row(i, t)
        self._update_sel()

    def _add_task_row(self, i: int, t: dict):
        ctk.CTkCheckBox(
            self.list_box, text="", width=24, height=24,
            checkbox_width=18, checkbox_height=18, corner_radius=5,
            variable=t["var"], command=self._update_sel,
            fg_color=pair("primary"), hover_color=pair("primary_hover"),
            border_color=pair("input_border"), checkmark_color="#FFFFFF",
        ).grid(row=i, column=0, sticky="w", padx=(8, 0), pady=4)

        ctk.CTkLabel(
            self.list_box, text=t["label"], anchor="w",
            font=ctk.CTkFont(FONT, 12), text_color=pair("text"),
        ).grid(row=i, column=1, sticky="ew", padx=8)

        ctk.CTkLabel(
            self.list_box, text=t["size"], anchor="e", width=74,
            font=ctk.CTkFont(FONT, 11), text_color=pair("muted"),
        ).grid(row=i, column=2, sticky="e")

        if t["exists"]:
            state, color = ("内容相同", pair("ok")) if t["same"] else ("内容不同", pair("primary"))
        else:
            state, color = "新文件", pair("muted")
        ctk.CTkLabel(
            self.list_box, text=state, anchor="e", width=62,
            font=ctk.CTkFont(FONT, 11), text_color=color,
        ).grid(row=i, column=3, sticky="e", padx=(8, 8))

    def _will_download(self, t: dict) -> bool:
        """这一项本次会不会真的下载（跳过规则与 _collect_jobs 保持一致）。"""
        if not t["var"].get():
            return False
        if t["exists"] and t["same"] and self.auto_skip.get():
            return False
        return True

    def _set_all(self, flag: bool):
        for t in self.tasks:
            t["var"].set(bool(flag))
        self._update_sel()

    def _invert_all(self):
        for t in self.tasks:
            t["var"].set(not t["var"].get())
        self._update_sel()

    def _update_sel(self):
        total = len(self.tasks)
        sel = [t for t in self.tasks if t["var"].get()]
        will = [t for t in self.tasks if self._will_download(t)]
        nbytes = sum(t["bytes"] for t in will)
        self.lbl_sel.configure(
            text=(f"已选 {len(sel)}/{total}　待下载 {len(will)} 个　约 {core.human(nbytes)}"
                  if total else "")
        )
        self._update_count()

    def _build_actions(self, parent):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=(10, 8))

        self.btn_scan = ctk.CTkButton(
            row,
            text="扫描",
            width=104,
            height=40,
            corner_radius=11,
            font=ctk.CTkFont(FONT, 13, "bold"),
            fg_color=pair("secondary"),
            hover_color=pair("secondary_hover"),
            text_color=pair("text"),
            text_color_disabled=pair("muted"),
            command=self.scan,
        )
        self.btn_scan.pack(side="left")

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
        self.btn_start.pack(side="left", padx=(10, 0))

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
        card = Card(parent, "⑤ 日志")
        card.pack(fill="x", pady=(8, 0))

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

    def _update_count(self):
        n = len(core.extract_objectids(self.txt_in.get("1.0", "end")))
        m = len(self.tasks)
        parts = []
        if n:
            parts.append(f"输入 {n} 个")
        if m:
            parts.append(f"列表 {m} 个")
        self.lbl_count.configure(text="　".join(parts) if parts else "")

    def paste_clip(self):
        try:
            self.txt_in.insert("end", self.clipboard_get().rstrip() + "\n")
        except Exception:  # noqa: BLE001
            messagebox.showinfo("提示", "剪贴板里没有文本")
        self._update_count()

    def clear_input(self):
        self.txt_in.delete("1.0", "end")
        self._update_count()

    def import_file(self):
        p = filedialog.askopenfilename(
            title="选择链接文件", filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")]
        )
        if p:
            with open(p, encoding="utf-8-sig", errors="replace") as f:
                self.txt_in.insert("end", f.read().rstrip() + "\n")
            self._update_count()

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

    # ---------------- 扫描 ----------------
    def _resolve_cookie(self) -> tuple[str | None, str]:
        """定登录态：手填 Cookie 优先，其次借本机 Firefox 的。返回 (cookie, 来源说明)。"""
        manual = self.cookie.get().strip()
        if manual:
            return manual, "手动 Cookie"
        fx = core.firefox_cookie_header()
        if fx:
            return fx, "Firefox Cookie"
        return None, "无"

    def _collect_jobs(self) -> list[tuple[str, str, str | None, bool]]:
        """汇总要真正下载的任务: (objectid, 保存目录, 文件名提示, 是否覆盖)。"""
        base = os.path.abspath(self.outdir.get())
        auto_skip = bool(self.auto_skip.get())
        jobs: list[tuple[str, str, str | None, bool]] = []
        for t in self.tasks:
            if not self._will_download(t):
                continue
            # 已经存在（且要么内容不同、要么关了自动跳过）→ 覆盖重下
            jobs.append((t["oid"], t["outdir"] or base, t["name"] or None, bool(t["exists"])))

        # 输入框里没进列表的裸 objectid 也一起下（老用法）。
        # 这条路径没有本地查重信息，交给 process_one 按字节数判断是否跳过。
        known = {t["oid"] for t in self.tasks}
        for oid in core.extract_objectids(self.txt_in.get("1.0", "end")):
            if oid not in known:
                jobs.append((oid, base, None, not auto_skip))

        seen: set[tuple[str, str]] = set()
        uniq: list[tuple[str, str, str | None, bool]] = []
        for j in jobs:
            if (j[0], j[1]) in seen:
                continue
            seen.add((j[0], j[1]))
            uniq.append(j)
        return uniq

    def scan(self):
        """扫描输入框里的课程页 / 预览页 / objectid，把文件列进任务列表。"""
        if self.scan_worker and self.scan_worker.is_alive():
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "正在下载，等下载结束再扫描。")
            return
        raw = self.txt_in.get("1.0", "end")
        urls = core.extract_course_urls(raw)
        oids = core.extract_objectids(raw)
        if not urls and not oids:
            messagebox.showwarning(
                "没找到内容",
                "请粘贴课程页链接（带 courseid / clazzid）、预览页链接，或 32 位 objectid。",
            )
            return

        cookie, src = self._resolve_cookie()
        base = os.path.abspath(self.outdir.get())

        self._clear_tasks()
        self.btn_scan.configure(state="disabled")
        self.btn_start.configure(state="disabled")
        self.lbl.configure(text="扫描中…", text_color=pair("muted"))
        self.log(f"\n开始扫描：{len(urls)} 个课程页 + {len(oids)} 个单独文件", "head")
        self.log(f"登录态: {src}", "dim")

        self.scan_worker = threading.Thread(
            target=self._run_scan, args=(urls, oids, cookie, base), daemon=True
        )
        self.scan_worker.start()

    def _run_scan(self, urls, oids, cookie, base):
        def say(msg, tag="dim"):
            self.q.put(("scan_log", (msg, tag)))

        try:
            if urls and not cookie:
                say("没读到登录态 —— 请先在 Firefox 登录学习通，"
                    "或把浏览器的 Cookie 粘到「Cookie」框里", "err")
            for u in urls:
                say(f"[课程] {u}", "head")
                try:
                    sc = core.scan_course(cookie, u, self.timeout,
                                          progress=lambda m: say(m))
                except Exception as e:  # noqa: BLE001
                    say(f"扫描失败：{e}", "err")
                    continue
                outdir = os.path.join(base, core.sanitize(sc["name"], sc["courseid"]))
                try:
                    core.resolve_course_files(cookie, sc["files"], outdir,
                                              self.timeout, progress=lambda m: say(m))
                except Exception as e:  # noqa: BLE001
                    say(f"查重失败（不影响下载）：{e}", "err")
                self.q.put(("scan_files", (sc["name"], outdir, sc["files"])))

            for oid in oids:
                try:
                    d = core.describe_object(cookie, oid, self.timeout)
                except Exception as e:  # noqa: BLE001
                    say(f"{oid} 取信息失败：{e}", "err")
                    continue
                d.update({"chapter": "", "unit": ""})
                self.q.put(("scan_files", ("", base, [d])))
        except Exception as e:  # noqa: BLE001
            say(f"扫描出错：{e}", "err")
        self.q.put(("scan_done", None))

    def _on_scan_files(self, course: str, outdir: str, files: list[dict]):
        for f in files:
            name = f.get("name") or f["objectid"]
            if f.get("chapter"):
                label = f"{f.get('unit') or '-'} / {f['chapter']} · {name}"
            else:
                label = name
            if len(label) > 72:
                label = label[:70] + "…"
            exists = bool(f.get("exists"))
            same = f.get("same")
            self.tasks.append({
                "oid": f["objectid"],
                "name": name,
                "label": label,
                "size": f.get("hsize") or core.human(int(f.get("length") or 0)),
                "bytes": int(f.get("length") or 0) or core.parse_hsize(f.get("hsize") or ""),
                "course": course,
                "outdir": outdir,
                "path": f.get("path") or "",
                "exists": exists,
                "same": same,
                "var": ctk.BooleanVar(value=True),
            })
        self._render_tasks()

    def _scan_finish(self):
        self.btn_scan.configure(state="normal")
        self.btn_start.configure(state="normal")
        n = len(self.tasks)
        same = sum(1 for t in self.tasks if t["exists"] and t["same"])
        diff = sum(1 for t in self.tasks if t["exists"] and not t["same"])
        self.lbl.configure(
            text=(f"共 {n} 个" if n else "就绪"),
            text_color=pair("primary") if n else pair("muted"),
        )
        if n:
            self.log(f"扫描完成：{n} 个文件（本地已存在 {same + diff} 个：相同 {same}、"
                     f"不同 {diff}）。勾选后点「开始下载」。", "head")
        else:
            self.log("扫描完成，但没找到任何附件。", "err")

    # ---------------- 下载 ----------------
    def start(self):
        if self.worker and self.worker.is_alive():
            return
        if self.scan_worker and self.scan_worker.is_alive():
            messagebox.showinfo("提示", "正在扫描课程，等扫描结束再下载。")
            return

        job_list = self._collect_jobs()
        if not job_list:
            if core.extract_course_urls(self.txt_in.get("1.0", "end")):
                messagebox.showwarning(
                    "请先扫描",
                    "检测到课程页链接，请先点「扫描」把文件列出来再下载。",
                )
            else:
                messagebox.showwarning(
                    "没找到链接",
                    "请粘贴预览页链接 / 32 位 objectid，\n"
                    "或粘课程页链接后先点「扫描」。",
                )
            return

        mode = MODE_LABELS.get(self.mode.get(), "orig")
        want_original = mode in ("orig", "both")
        want_pdf = mode in ("pdf", "both")
        conc = max(1, int(self.jobs.get() or 4))
        auto_skip = bool(self.auto_skip.get())
        manual = self.cookie.get().strip()
        cookie = manual or core.firefox_cookie_header() or None

        for _, d, _, _ in job_list:
            os.makedirs(d, exist_ok=True)

        save_settings(
            {
                "outdir": self.outdir.get(),
                "mode": mode,
                "jobs": conc,
                "auto_skip": auto_skip,
                "cookie": self.cookie.get(),
                "appearance": self.appearance.get(),
            }
        )

        self.stop_evt.clear()
        self.total = len(job_list)
        self.done = self.ok = self.fail = 0
        self.t0 = time.time()
        self._progress(0)
        self.lbl.configure(text=f"0 / {self.total}", text_color=pair("muted"))
        self.btn_start.configure(state="disabled")
        self.btn_scan.configure(state="disabled")
        self.btn_stop.configure(state="normal")

        dirs = sorted({d for _, d, _, _ in job_list})
        self.log(f"\n共 {len(job_list)} 个文件 → {dirs[0] if len(dirs) == 1 else '多目录'}", "head")
        self.log(f"模式: {self.mode.get()}　并发: {conc}", "dim")
        self.log(
            "登录态: " + ("手动 Cookie" if manual else ("Firefox Cookie" if cookie else "无"))
            + ("" if want_original else "　(仅下载 PDF)"),
            "dim",
        )
        skipped = [t for t in self.tasks
                   if t["var"].get() and t["exists"] and t["same"] and auto_skip]
        if skipped:
            self.log(f"自动跳过 {len(skipped)} 个内容相同的文件（可在选项里关掉）", "dim")

        self.worker = threading.Thread(
            target=self._run,
            args=(job_list, want_original, want_pdf, cookie, conc),
            daemon=True,
        )
        self.worker.start()

    def _run(self, job_list, want_original, want_pdf, cookie, conc):
        with ThreadPoolExecutor(max_workers=conc) as ex:
            futs = {}
            for oid, outdir, hint, overwrite in job_list:
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
                        hint,
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
                elif kind == "scan_log":
                    self.log(payload[0], payload[1])
                elif kind == "scan_files":
                    self._on_scan_files(*payload)
                elif kind == "scan_done":
                    self._scan_finish()
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
        self.btn_scan.configure(state="normal")
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
        busy = (self.worker and self.worker.is_alive()) or (
            self.scan_worker and self.scan_worker.is_alive()
        )
        if busy:
            if not messagebox.askokcancel("正在忙", "还有任务在跑，确定要退出吗？"):
                return
            self.stop_evt.set()
        self.destroy()


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
