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
import re
import sys
import threading
import time
import tkinter as tk
import webbrowser
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

# ⓘ 悬停说明。长文字一律塞这里，界面上只留一个圆圈 i。
COOKIE_HINT = (
    "粘贴浏览器的 Cookie 请求头即可 —— 任何浏览器都行，不用装 Firefox。\n\n"
    "怎么拿：\n"
    "1. 浏览器打开学习通并登录\n"
    "2. 按 F12，切到 Network / 网络\n"
    "3. 刷新页面，点任意一条 chaoxing.com 的请求\n"
    "4. 在 Request Headers 里找到 Cookie: 那一整行，全选复制\n"
    "5. 回来点「粘贴」，再重新扫描\n\n"
    "注意：Cookie 会过期，过一阵要重新弄一次。\n"
    "装了 Firefox 的话不用管这个 —— 工具会自动读它的登录态。"
)
LOGIN_HELP = (
    "扫描课程要证明「你有权限看这门课」。\n"
    "课程链接里只有 courseid / clazzid 这些编号，不含登录凭证 ——\n"
    "缺了登录态，服务器会直接返回登录页，什么也扫不到。\n\n"
    "任选一种方式：\n\n"
    "①  用 Firefox（最省事，弄一次就够）\n"
    "     在 Firefox 里打开学习通登录一次，然后回来重新扫描。\n"
    "     工具会自动读它的登录状态，以后都不用再管。\n\n"
    "②  手贴 Cookie（任何浏览器都行，不用装 Firefox）\n"
    "     1. 浏览器打开学习通并登录\n"
    "     2. 按 F12，切到 Network / 网络\n"
    "     3. 刷新页面，点任意一条 chaoxing.com 的请求\n"
    "     4. 在 Request Headers 里找到 Cookie: 那一整行，全选复制\n"
    "     5. 回来点下面的「从剪贴板粘贴 Cookie」，再重新扫描\n\n"
    "Cookie 会过期，过一阵要重新弄一次。"
)
SKIP_HINT = (
    "开启后，本地已有且「内容相同」的文件会被自动跳过。\n\n"
    "「内容相同」的判定：字节数一致，且前 64KB 的 md5 一致。\n"
    "靠 HTTP Range 只取 64KB，不用整包重下。\n\n"
    "关掉的话，同名文件一律重下覆盖。"
)
LEVELS_HINT = (
    "勾中哪一级，才会真的建对应的文件夹。\n"
    "拿「章节」里的一个文件举例，四级的取值是：\n\n"
    "    课程名 → 261学期 示例课程\n"
    "    来源   → 章节（或者 资料）\n"
    "    分组   → 教学课件\n"
    "    章节名 → 第一章 操作系统引论\n\n"
    "四个全勾：\n"
    "    …/261学期…/章节/教学课件/第一章 操作系统引论/xxx.pptx\n"
    "四个全不勾：直接放在保存目录里。\n\n"
    "「资料」没有「分组」这一级；它的文件夹可能是多级的，\n"
    "勾了「章节名」会逐级建出来。"
)

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


def round_rect(cv: tk.Canvas, x1: float, y1: float, x2: float, y2: float,
               r: float, **kw):
    """在 Canvas 上画圆角矩形（Tk 没有原生圆角，用 smooth 多边形凑）。"""
    pts = [
        x1 + r, y1, x2 - r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y1 + r, x2, y2 - r, x2, y2 - r, x2, y2, x2 - r, y2,
        x2 - r, y2, x1 + r, y2, x1 + r, y2, x1, y2, x1, y2 - r,
        x1, y2 - r, x1, y1 + r, x1, y1 + r, x1, y1, x1 + r, y1,
    ]
    return cv.create_polygon(pts, smooth=True, **kw)


def disp_width(text: str) -> int:
    """粗略显示宽度：CJK 字符按 2 算，其余按 1。"""
    return sum(2 if ord(c) > 0x2E80 else 1 for c in (text or ""))


def elide(text: str, limit: int) -> str:
    """按显示宽度截断（不是按字符数），超出补省略号。

    任务列表那一列宽度有限，中文按字符数截会严重低估。
    """
    if disp_width(text) <= limit:
        return text
    out: list[str] = []
    w = 0
    for ch in text:
        cw = 2 if ord(ch) > 0x2E80 else 1
        if w + cw > limit - 1:
            break
        out.append(ch)
        w += cw
    return "".join(out) + "…"


def elide_mid(text: str, limit: int) -> str:
    """从中间截断，保住头尾 —— 文件名必须留住扩展名。"""
    if disp_width(text) <= limit:
        return text
    half = max(1, (limit - 1) // 2)
    head: list[str] = []
    w = 0
    for ch in text:
        cw = 2 if ord(ch) > 0x2E80 else 1
        if w + cw > half:
            break
        head.append(ch)
        w += cw
    tail: list[str] = []
    w = 0
    for ch in reversed(text):
        cw = 2 if ord(ch) > 0x2E80 else 1
        if w + cw > half:
            break
        tail.append(ch)
        w += cw
    return "".join(head) + "…" + "".join(reversed(tail))


def build_label(unit: str, chapter: str, name: str, limit: int = 74) -> str:
    """任务列表里一行的文字：`分组 / 章节 · 文件名`。

    来源（章节 / 资料）由上面的分组头表达，这里不再重复。
    空间不够时**先压路径、再中间截断文件名**，保证扩展名还在。
    """
    path = " / ".join(p for p in (unit, chapter) if p)
    if not path:
        return elide_mid(name, limit)
    head = elide(path, 40)
    budget = limit - disp_width(head) - 3  # 减去 " · "
    if budget < 22:
        head = elide(path, 20)
        budget = limit - disp_width(head) - 3
    return f"{head} · {elide_mid(name, max(14, budget))}"


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


class LevelChips(ctk.CTkFrame):
    """一排「可勾选标签」，用来选目录层级。

    没用 CTkCheckBox 是因为它的最小宽度收不住：width 填 70，实际请求宽度是 122
    （勾选框 + 内部间距），四个排一行直接把左列撑爆。
    这里用一排 CTkButton 自绘，选中态靠底色区分（跟 ToggleGroup 一个路子）。
    """

    def __init__(self, master, items, command=None, **kw):
        super().__init__(master, fg_color="transparent", **kw)
        self.items = list(items)          # [(标签, BooleanVar), ...]
        self.command = command
        self.font = ctk.CTkFont(FONT, 12)
        self.btns: dict[str, ctk.CTkButton] = {}
        for text, _var in self.items:
            b = ctk.CTkButton(
                self, text=text, width=self.font.measure(text) + 22, height=28,
                corner_radius=8, font=self.font,
                command=lambda t=text: self.toggle(t),
            )
            b.pack(side="left", padx=(0, 5))
            self.btns[text] = b
        self._restyle()

    def toggle(self, text: str):
        for t, var in self.items:
            if t == text:
                var.set(not var.get())
                break
        self._restyle()
        if self.command:
            self.command()

    def _restyle(self):
        for text, var in self.items:
            b = self.btns[text]
            if var.get():
                b.configure(fg_color=pair("primary"), hover_color=pair("primary_hover"),
                            text_color="#FFFFFF")
            else:
                b.configure(fg_color=pair("secondary"), hover_color=pair("secondary_hover"),
                            text_color=pair("text"))


class HintIcon(ctk.CTkLabel):
    """一个圆圈 i，鼠标悬停弹出说明气泡。

    CustomTkinter 没有 tooltip。这里用 overrideredirect 的普通 Tk Toplevel 实现 ——
    不走 CTkToplevel 是为了躲开它自己的缩放/延迟显示逻辑，位置更好控制。
    气泡每次悬停现建现毁：悬停是低频操作，省得再去处理主题切换时的重绘。
    """

    def __init__(self, master, text: str, size: int = 18, **kw):
        kw.setdefault("width", size)
        kw.setdefault("height", size)
        kw.setdefault("corner_radius", size // 2)
        kw.setdefault("font", ctk.CTkFont(FONT, 11, "bold"))
        kw.setdefault("fg_color", pair("secondary"))
        kw.setdefault("text_color", pair("muted"))
        super().__init__(master, text="i", **kw)
        self.hint_text = text
        self._tip: tk.Toplevel | None = None
        self.bind("<Enter>", self._pop)
        self.bind("<Leave>", self._drop)
        self.bind("<Button-1>", self._pop)

    def _pop(self, _evt=None):
        self._drop()
        dark = ctk.get_appearance_mode().lower() == "dark"
        bg = "#20252C" if dark else "#FFFFFF"
        fg = "#D7DEE7" if dark else "#2F4858"
        bd = "#3E4A56" if dark else "#C9D3DE"
        # 圆角外面这圈用主窗口底色，看着就像透明
        outside = "#16191E" if dark else "#E9EDF2"

        # 关键：普通 Tk 字体**不跟** CTk 的 DPI 缩放走。CTk 的 12 在 175% 下
        # 是 21px，这里直接写成 Tk 的 10 磅就只剩一半大。所以按 scaling 换算成
        # 像素字号（Tk 里负数=像素），padding / 圆角 / 换行宽度一并跟着缩。
        try:
            scale = ctk.ScalingTracker.get_widget_scaling(self) or 1.0
        except Exception:  # noqa: BLE001
            scale = 1.0

        def px(v: float) -> int:
            return max(1, int(round(v * scale)))

        tip = tk.Toplevel(self)
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        tip.configure(bg=outside)

        # 注意：create_window 用的 widget 必须是这个 Canvas 的子控件，
        # 挂到 Toplevel 上的话窗口项不会渲染（表现为气泡是空的一块）。
        cv = tk.Canvas(tip, bg=outside, highlightthickness=0, bd=0)
        cv.pack()
        label = tk.Label(
            cv, text=self.hint_text, justify="left", anchor="nw",
            bg=bg, fg=fg, bd=0, highlightthickness=0,
            wraplength=px(330), font=(FONT, -px(12)),
        )
        label.update_idletasks()
        lw, lh = label.winfo_reqwidth(), label.winfo_reqheight()
        m, r = px(14), px(10)

        cv.configure(width=lw + m * 2, height=lh + m * 2)
        round_rect(cv, 0.5, 0.5, lw + m * 2 - 0.5, lh + m * 2 - 0.5, r,
                   fill=bg, outline=bd, width=max(1, px(1)))
        cv.create_window(m, m, anchor="nw", window=label)
        tip.update_idletasks()

        w, h = tip.winfo_reqwidth(), tip.winfo_reqheight()
        x = self.winfo_rootx() + self.winfo_width() + px(8)
        y = self.winfo_rooty() - px(8)
        x = min(x, tip.winfo_screenwidth() - w - px(12))
        y = min(y, tip.winfo_screenheight() - h - px(12))
        tip.geometry(f"+{max(px(8), x)}+{max(px(8), y)}")
        self._tip = tip

    def _drop(self, _evt=None):
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:  # noqa: BLE001
                pass
            self._tip = None


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
        # 目录层级：勾中的才会真的建目录。默认只建「课程名」一级，跟老行为一致。
        lv = cfg.get("levels") or {}
        self.lv_course = ctk.BooleanVar(value=bool(lv.get("course", True)))
        self.lv_source = ctk.BooleanVar(value=bool(lv.get("source", False)))
        self.lv_unit = ctk.BooleanVar(value=bool(lv.get("unit", False)))
        self.lv_chapter = ctk.BooleanVar(value=bool(lv.get("chapter", False)))

        self.q: queue.Queue = queue.Queue()
        self.stop_evt = threading.Event()
        self.worker: threading.Thread | None = None
        self.scan_worker: threading.Thread | None = None
        # 任务列表（扫描结果）。每个元素见 _add_task_row 里的字段说明。
        self.tasks: list[dict] = []
        self.groups: dict[str, dict] = {}     # 分组名 → {var, btn, members}
        self.collapsed: set[str] = set()      # 被折叠起来的分组名
        # 本次扫描时的「保存目录 + 层级勾选」，用来判断查重结果是否还有效
        self.scan_key = ""
        self._fx_cookie_count = 0             # 本机 Firefox 里 chaoxing cookie 的条数
        self._scan_login_failed = False       # 本次扫描是否因登录态中断
        self.total = self.done = self.ok = self.fail = 0
        self.t0 = 0.0
        self._last_mode = ""

        self._build()
        self._apply_appearance(self.appearance.get())
        self._restyle_for_theme()
        self._update_preview()
        self.outdir.trace_add("write", lambda *_: self._update_preview())
        self._refresh_firefox_cookies()
        self._update_auth_label()
        self.cookie.trace_add("write", lambda *_: self._update_auth_label())
        self.after(100, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- 窗口 ----------------
    def _fit_geometry(self, want_w: int, want_h: int):
        """按真实物理分辨率 + CTk 缩放换算，保证窗口一定放得下。

        只设尺寸不够：窗口默认由系统随便摆，高一点的窗口会被摆到屏幕外，
        底部（操作行/日志）就看不见了。所以这里连位置一起算，居中并夹在屏幕内。
        """
        try:
            sw = ctypes.windll.user32.GetSystemMetrics(0)
            sh = ctypes.windll.user32.GetSystemMetrics(1)
            scale = ctk.ScalingTracker.get_widget_scaling(self) or 1.0
            # 预留标题栏 + 任务栏的空间（逻辑像素）
            avail_w = int(sw / scale) - 40
            avail_h = int(sh / scale) - 110
            want_w = max(860, min(want_w, avail_w))
            want_h = max(560, min(want_h, avail_h))
            x = max(0, (avail_w - want_w) // 2) + 20
            y = max(0, (avail_h - want_h) // 2) + 20
            self.geometry(f"{want_w}x{want_h}+{x}+{y}")
        except Exception:  # noqa: BLE001
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
            text="预览页链接 / objectid / 整门课程页链接 —— 课程页可一次扫出「章节任务点 + 资料页文件」",
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
        ).pack(side="left", padx=(16, 0))
        HintIcon(row2, SKIP_HINT).pack(side="left", padx=(6, 0))

        row3 = ctk.CTkFrame(card, fg_color="transparent")
        row3.pack(fill="x", padx=14, pady=(0, 2))
        ctk.CTkLabel(
            row3, text="Cookie", font=ctk.CTkFont(FONT, 12),
            text_color=pair("muted"), height=32,
        ).pack(side="left")
        HintIcon(row3, COOKIE_HINT).pack(side="left", padx=(6, 8))
        ctk.CTkEntry(
            row3,
            textvariable=self.cookie,
            height=32,
            corner_radius=9,
            border_width=1,
            border_color=pair("input_border"),
            fg_color=pair("input"),
            text_color=pair("text"),
            placeholder_text="留空则自动读 Firefox",
            placeholder_text_color=pair("muted"),
            font=ctk.CTkFont(FONT, 12),
        ).pack(side="left", fill="x", expand=True)
        self._btn(row3, "粘贴", self.paste_cookie, width=56).pack(side="left", padx=(6, 0))

        self.lbl_auth = ctk.CTkLabel(
            card, text="", anchor="w", height=16,
            font=ctk.CTkFont(FONT, 11), text_color=pair("muted"),
        )
        self.lbl_auth.pack(fill="x", padx=14, pady=(0, 8))

        # 目录层级：勾中的那一级才会真的建成文件夹
        row4 = ctk.CTkFrame(card, fg_color="transparent")
        row4.pack(fill="x", padx=14, pady=(0, 2))
        ctk.CTkLabel(
            row4, text="目录层级", font=ctk.CTkFont(FONT, 12),
            text_color=pair("muted"), height=28,
        ).pack(side="left", padx=(0, 4))
        HintIcon(row4, LEVELS_HINT, size=18).pack(side="left", padx=(0, 8))
        self.lv_chips = LevelChips(
            row4,
            items=[
                ("课程名", self.lv_course),
                ("来源", self.lv_source),
                ("分组", self.lv_unit),
                ("章节名", self.lv_chapter),
            ],
            command=self._update_preview,
        )
        self.lv_chips.pack(side="left")

        self.lbl_preview = ctk.CTkLabel(
            card, text="", anchor="w", height=16,
            font=ctk.CTkFont(FONT, 11), text_color=pair("muted"),
        )
        self.lbl_preview.pack(fill="x", padx=14, pady=(0, 10))

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
        self.groups = {}
        self._update_sel()
        self._update_preview()

    def _render_tasks(self):
        """按 self.tasks 重建列表：先分组头（章节 / 资料…），再列成员。"""
        for w in self.list_box.winfo_children():
            w.destroy()
        self.empty_hint = None
        self.groups = {}
        if not self.tasks:
            self._show_empty_hint()
            self._update_sel()
            self._update_preview()
            return

        order: list[str] = []
        for t in self.tasks:
            if t["group"] not in order:
                order.append(t["group"])

        r = 0
        for grp in order:
            members = [t for t in self.tasks if t["group"] == grp]
            self._add_group_row(r, grp, members)
            r += 1
            for t in members:
                t["_rows"] = self._add_task_row(r, t)
                r += 1
        self._apply_group_visibility()
        self._update_sel()
        self._update_preview()

    def _add_group_row(self, i: int, grp: str, members: list[dict]):
        """分组头：整行做成一条可点折叠的横条，组勾选框放在横条**里面**。

        勾选框不能单独占第 0 列 —— 那一列的宽度会被下面的子行（缩进过）撑开，
        结果就是勾选框贴着左边、横条却从很靠右的地方开始，看着像两段缩进。
        """
        bar = ctk.CTkFrame(self.list_box, fg_color=pair("secondary"), corner_radius=8)
        bar.grid(row=i, column=0, columnspan=4, sticky="ew", padx=(8, 8), pady=(10, 2))

        var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            bar, text="", width=24, height=24,
            checkbox_width=18, checkbox_height=18, corner_radius=5,
            variable=var, command=lambda g=grp: self._toggle_group_all(g),
            fg_color=pair("primary"), hover_color=pair("primary_hover"),
            border_color=pair("input_border"), checkmark_color="#FFFFFF",
        ).pack(side="left", padx=(10, 0), pady=3)

        lbl = ctk.CTkLabel(
            bar, text="", anchor="w",
            font=ctk.CTkFont(FONT, 12, "bold"), text_color=pair("text"),
        )
        lbl.pack(side="left", fill="x", expand=True, padx=(10, 12), pady=3)

        # 点横条 = 折叠/展开；勾选框自己处理自己的点击
        for w in (bar, lbl):
            w.bind("<Button-1>", lambda e, g=grp: self._toggle_group(g))
            w.bind("<Enter>", lambda e, b=bar: b.configure(fg_color=pair("secondary_hover")))
            w.bind("<Leave>", lambda e, b=bar: b.configure(fg_color=pair("secondary")))
        self.groups[grp] = {"var": var, "lbl": lbl, "bar": bar, "members": members}

    def _add_task_row(self, i: int, t: dict) -> list:
        """一行文件。返回该行所有控件，折叠时要靠它们整行收起。"""
        ws: list = []
        cb = ctk.CTkCheckBox(
            self.list_box, text="", width=24, height=24,
            checkbox_width=18, checkbox_height=18, corner_radius=5,
            variable=t["var"], command=self._update_sel,
            fg_color=pair("primary"), hover_color=pair("primary_hover"),
            border_color=pair("input_border"), checkmark_color="#FFFFFF",
        )
        cb.grid(row=i, column=0, sticky="w", padx=(32, 0), pady=3)
        ws.append(cb)

        lbl = ctk.CTkLabel(
            self.list_box, text=t["label"], anchor="w",
            font=ctk.CTkFont(FONT, 12), text_color=pair("text"),
        )
        lbl.grid(row=i, column=1, sticky="ew", padx=8)
        ws.append(lbl)

        sz = ctk.CTkLabel(
            self.list_box, text=t["size"], anchor="e", width=74,
            font=ctk.CTkFont(FONT, 11), text_color=pair("muted"),
        )
        sz.grid(row=i, column=2, sticky="e")
        ws.append(sz)

        if t["exists"]:
            state, color = ("内容相同", pair("ok")) if t["same"] else ("内容不同", pair("primary"))
        else:
            state, color = "新文件", pair("muted")
        st = ctk.CTkLabel(
            self.list_box, text=state, anchor="e", width=62,
            font=ctk.CTkFont(FONT, 11), text_color=color,
        )
        st.grid(row=i, column=3, sticky="e", padx=(8, 8))
        ws.append(st)
        return ws

    # ---------------- 分组折叠 ----------------
    def _toggle_group(self, grp: str):
        if grp in self.collapsed:
            self.collapsed.discard(grp)
        else:
            self.collapsed.add(grp)
        self._apply_group_visibility()
        self._paint_groups()

    def _toggle_group_all(self, grp: str):
        """组级勾选框：全选 / 全不选这一组。"""
        members = [t for t in self.tasks if t["group"] == grp]
        target = not all(t["var"].get() for t in members)
        for t in members:
            t["var"].set(target)
        self._update_sel()

    def _apply_group_visibility(self):
        for t in self.tasks:
            show = t["group"] not in self.collapsed
            for w in t.get("_rows", []):
                if show:
                    w.grid()          # 无参调用 = 还原上次的 grid 配置
                else:
                    w.grid_remove()   # 行会跟着塌掉

    def _paint_groups(self):
        for grp, info in self.groups.items():
            members = info["members"]
            sel = sum(1 for t in members if t["var"].get())
            will = [t for t in members if self._will_download(t)]
            size = sum(t["bytes"] for t in will)
            arrow = "▸" if grp in self.collapsed else "▾"
            info["lbl"].configure(
                text=f"{arrow} {grp}　{len(members)} 个 · 待下载 {len(will)} · 约 {core.human(size)}"
            )
            info["var"].set(bool(members) and sel == len(members))

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
        self._paint_groups()
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

    # ---------------- 登录态 ----------------
    def paste_cookie(self, close_dialog=None):
        """从剪贴板填 Cookie。允许直接粘整段请求头，自动只取 Cookie: 后面那截。"""
        try:
            text = self.clipboard_get().strip()
        except Exception:  # noqa: BLE001
            messagebox.showinfo("提示", "剪贴板里没有文本")
            return
        if not text:
            messagebox.showinfo("提示", "剪贴板是空的")
            return
        m = re.search(r"(?im)^\s*cookie\s*:\s*(.+)$", text)
        if m:
            text = m.group(1).strip()
        text = " ".join(text.split())          # 折行、多余空白都压掉
        if "=" not in text and not messagebox.askyesno(
            "看着不像 Cookie",
            "粘进来的内容里没有 = 号，可能不是 Cookie 请求头。\n确定要用它吗？",
        ):
            return
        self.cookie.set(text)
        self.log(f"已填入手动 Cookie（{len(text)} 字符）", "ok")
        if close_dialog is not None:
            close_dialog.destroy()

    def _open_login_page(self):
        try:
            webbrowser.open("https://i.mooc.chaoxing.com/")
        except Exception:  # noqa: BLE001
            pass

    def _update_auth_label(self):
        if not hasattr(self, "lbl_auth"):
            return
        manual = self.cookie.get().strip()
        if manual:
            txt, color = f"登录态：手动 Cookie（{len(manual)} 字符）", pair("ok")
        elif self._fx_cookie_count:
            txt = f"登录态：本机 Firefox（{self._fx_cookie_count} 条 chaoxing cookie）"
            color = pair("ok")
        else:
            txt = "登录态：无 —— 扫描会拿到登录页，点左边 ⓘ 看怎么弄"
            color = pair("primary")
        self.lbl_auth.configure(text=txt, text_color=color)

    def _refresh_firefox_cookies(self):
        try:
            _, rows = core.load_firefox_cookies()
            self._fx_cookie_count = len(rows)
        except Exception:  # noqa: BLE001
            self._fx_cookie_count = 0

    def _show_login_help(self):
        """没登录态时的引导弹窗。"""
        dlg = ctk.CTkToplevel(self)
        dlg.title("需要学习通的登录态")
        dlg.configure(fg_color=pair("page"))
        dlg.resizable(False, False)
        dlg.transient(self)
        try:
            dlg.after(60, dlg.lift)
        except Exception:  # noqa: BLE001
            pass

        wrap = ctk.CTkFrame(dlg, fg_color=pair("card"), corner_radius=12,
                            border_width=1, border_color=pair("border"))
        wrap.pack(fill="both", expand=True, padx=14, pady=14)

        ctk.CTkLabel(
            wrap, text="需要学习通的登录态", anchor="w", height=26,
            font=ctk.CTkFont(FONT, 15, "bold"), text_color=pair("primary"),
        ).pack(fill="x", padx=16, pady=(14, 4))
        ctk.CTkLabel(
            wrap, text=LOGIN_HELP, justify="left", anchor="w",
            font=ctk.CTkFont(FONT, 12), text_color=pair("text"), wraplength=470,
        ).pack(fill="x", padx=16)

        row = ctk.CTkFrame(wrap, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(14, 14))
        self._btn(row, "从剪贴板粘贴 Cookie", lambda: self.paste_cookie(dlg),
                  width=172).pack(side="left")
        self._btn(row, "打开学习通登录页", self._open_login_page,
                  width=150).pack(side="left", padx=8)
        self._btn(row, "关闭", dlg.destroy, width=68).pack(side="left")

        dlg.update_idletasks()
        w, h = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
        x = self.winfo_rootx() + (self.winfo_width() - w) // 2
        y = self.winfo_rooty() + (self.winfo_height() - h) // 3
        dlg.geometry(f"+{max(20, x)}+{max(20, y)}")

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

    # ---------------- 落点目录 ----------------
    def _levels_on(self) -> str:
        """层级勾选的指纹，用来判断「查重结果是不是在当前设置下算的」。"""
        return "".join(
            "1" if v.get() else "0"
            for v in (self.lv_course, self.lv_source, self.lv_unit, self.lv_chapter)
        )

    def _dir_key(self) -> str:
        return f"{os.path.abspath(self.outdir.get())}|{self._levels_on()}"

    def _job_dir(self, base: str, t: dict) -> str:
        """按勾选的层级拼出这一项的落点目录。勾中哪级才建哪级。"""
        parts: list[str] = []
        if self.lv_course.get() and t.get("subdir"):
            parts.append(t["subdir"])
        if self.lv_source.get() and t.get("group"):
            parts.append(core.sanitize(t["group"], "其他"))
        if self.lv_unit.get() and t.get("unit"):
            parts.append(core.sanitize(t["unit"], "分组"))
        if self.lv_chapter.get() and t.get("chapter"):
            # 「资料」的 chapter 可能是多级文件夹路径（"a / b"），逐级建
            for seg in str(t["chapter"]).split(" / "):
                seg = core.sanitize(seg, "")
                if seg:
                    parts.append(seg)
        return os.path.join(base, *parts) if parts else base

    def _update_preview(self):
        """把当前层级拼出来的样子显示出来（拿列表第一项举例，没有就用样例）。"""
        if not hasattr(self, "lbl_preview"):
            return
        base = os.path.abspath(self.outdir.get())
        sample = self.tasks[0] if self.tasks else {
            "subdir": "课程名", "group": "章节",
            "unit": "分组", "chapter": "章节名", "name": "文件名.pptx",
        }
        rel = os.path.relpath(self._job_dir(base, sample), base)
        head = "" if rel == os.curdir else rel.replace(os.sep, "/") + "/"
        self.lbl_preview.configure(text="预览：" + elide(head + sample["name"], 62))

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

    def _collect_jobs(self) -> list[tuple[str, str, str, str | None, bool]]:
        """汇总要真正下载的任务: (类型, 载荷, 保存目录, 文件名提示, 是否覆盖)。

        类型 "oid" → 载荷是 objectid；"data" → 载荷是「资料」页的下载直链。
        """
        base = os.path.abspath(self.outdir.get())
        auto_skip = bool(self.auto_skip.get())
        key = self._dir_key()
        jobs: list[tuple[str, str, str, str | None, bool]] = []
        for t in self.tasks:
            if not self._will_download(t):
                continue
            # 落点按**当前**的保存目录 + 层级勾选算，不能用扫描那一刻定下的路径 ——
            # 否则扫完再改保存目录/层级，文件还是会跑到旧地方去。
            job_dir = self._job_dir(base, t)
            # 只有「查重就是在当前这套设置下做的」才敢按 exists 覆盖；
            # 改过目录或层级的话按没查过处理，交给 process_one / download_data_one
            # 的字节数校验兜底，不会白重下。
            overwrite = bool(t["exists"]) and t.get("scan_key") == key
            jobs.append((t["kind"], t["payload"], job_dir, t["name"] or None, overwrite))

        # 输入框里没进列表的裸 objectid 也一起下（老用法）。
        # 这条路径没有本地查重信息，交给 process_one 按字节数判断是否跳过。
        known = {t["payload"] for t in self.tasks}
        for oid in core.extract_objectids(self.txt_in.get("1.0", "end")):
            if oid not in known:
                jobs.append(("oid", oid, base, None, not auto_skip))

        seen: set[tuple[str, str]] = set()
        uniq: list[tuple[str, str, str, str | None, bool]] = []
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
        # 课程页必须要登录态；纯预览链接/objectid 不需要，所以只在有课程页时拦
        if urls and not cookie:
            self.log("登录态：无 —— 课程页扫描需要登录，已弹出获取说明", "err")
            self._update_auth_label()
            self._show_login_help()
            return
        base = os.path.abspath(self.outdir.get())
        self.scan_key = self._dir_key()

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
                info = core.parse_course_url(u)
                if not info:
                    say("不是可识别的课程页链接（URL 里需要 courseid 与 clazzid）", "err")
                    continue
                try:
                    name = (core.fetch_course_name(cookie, u, self.timeout)
                            or f"课程{info['courseid']}")
                except core.LoginRequired:
                    self.q.put(("need_login", None))
                    continue
                except Exception:  # noqa: BLE001
                    name = f"课程{info['courseid']}"
                # 只传课程子目录名，绝对路径留给下载时按当前保存目录拼
                subdir = core.sanitize(name, info["courseid"])
                outdir = os.path.join(base, subdir)

                # ① 章节页的任务点附件
                try:
                    sc = core.scan_course(cookie, u, self.timeout, progress=lambda m: say(m))
                    say(f"章节任务点：{len(sc['files'])} 个")
                    core.resolve_course_files(cookie, sc["files"], outdir,
                                              self.timeout, progress=lambda m: say(m))
                    self.q.put(("scan_files", (name, subdir, sc["files"])))
                except core.LoginRequired:
                    self.q.put(("need_login", None))
                except Exception as e:  # noqa: BLE001
                    say(f"章节扫描失败：{e}", "err")

                # ② 「资料」页的文件
                try:
                    dfiles = core.scan_course_data(
                        cookie, info["courseid"], info["clazzid"], info["cpi"],
                        self.timeout, progress=lambda m: say(m))
                    say(f"课程资料：{len(dfiles)} 个")
                    core.resolve_course_files(cookie, dfiles, outdir,
                                              self.timeout, progress=lambda m: say(m))
                    self.q.put(("scan_files", (name, subdir, dfiles)))
                except core.LoginRequired:
                    self.q.put(("need_login", None))
                except Exception as e:  # noqa: BLE001
                    say(f"资料扫描失败：{e}", "err")

            for oid in oids:
                try:
                    d = core.describe_object(cookie, oid, self.timeout)
                except Exception as e:  # noqa: BLE001
                    say(f"{oid} 取信息失败：{e}", "err")
                    continue
                d.update({"source": "链接", "chapter": "", "unit": ""})
                self.q.put(("scan_files", ("", "", [d])))
        except Exception as e:  # noqa: BLE001
            say(f"扫描出错：{e}", "err")
        self.q.put(("scan_done", None))

    def _on_scan_files(self, course: str, subdir: str, files: list[dict]):
        for f in files:
            name = f.get("name") or f.get("objectid") or f.get("url") or "未命名"
            # 来源由分组头表达，这里只写 分组 / 章节 · 文件名
            label = build_label(f.get("unit") or "", f.get("chapter") or "", name)
            exists = bool(f.get("exists"))
            same = f.get("same")
            url = f.get("url") or ""
            self.tasks.append({
                "kind": "data" if url else "oid",
                "payload": url or f.get("objectid") or "",
                "oid": f.get("objectid") or "",
                "group": f.get("source") or "其他",
                "subdir": subdir,
                "unit": f.get("unit") or "",
                "chapter": f.get("chapter") or "",
                "scan_key": self.scan_key,
                "name": name,
                "label": label,
                "size": f.get("hsize") or core.human(int(f.get("length") or 0)),
                "bytes": int(f.get("length") or 0) or core.parse_hsize(f.get("hsize") or ""),
                "course": course,
                "path": f.get("path") or "",
                "exists": exists,
                "same": same,
                "var": ctk.BooleanVar(value=True),
            })
        self._render_tasks()

    def _scan_finish(self):
        self.btn_scan.configure(state="normal")
        self.btn_start.configure(state="normal")
        self._refresh_firefox_cookies()
        self._update_auth_label()
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
        elif self._scan_login_failed:
            self.log("扫描中断：先把登录态弄好，再点「扫描」重试。", "err")
        else:
            self.log("扫描完成，但没找到任何附件。", "err")
        self._scan_login_failed = False

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

        for _, _, d, _, _ in job_list:
            os.makedirs(d, exist_ok=True)

        save_settings(
            {
                "outdir": self.outdir.get(),
                "mode": mode,
                "jobs": conc,
                "auto_skip": auto_skip,
                "cookie": self.cookie.get(),
                "appearance": self.appearance.get(),
                "levels": {
                    "course": bool(self.lv_course.get()),
                    "source": bool(self.lv_source.get()),
                    "unit": bool(self.lv_unit.get()),
                    "chapter": bool(self.lv_chapter.get()),
                },
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

        dirs = sorted({d for _, _, d, _, _ in job_list})
        self.log(f"\n共 {len(job_list)} 个文件 → {dirs[0] if len(dirs) == 1 else '多目录'}", "head")
        self.log(f"模式: {self.mode.get()}　并发: {conc}", "dim")
        self.log(
            "登录态: " + ("手动 Cookie" if manual else ("Firefox Cookie" if cookie else "无"))
            + ("" if want_original else "　(仅下载 PDF)"),
            "dim",
        )
        if self.tasks and self.scan_key and self.scan_key != self._dir_key():
            self.log("保存目录或目录层级跟扫描时不一样：落点按现在的设置走；"
                     "列表里的「已存在 / 内容相同」是扫描时那次判断的，可能已经不准", "err")
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
            for kind, payload, outdir, hint, overwrite in job_list:
                if self.stop_evt.is_set():
                    break
                if kind == "data":
                    fut = ex.submit(core.download_data_one, payload, outdir, hint,
                                    cookie, self.timeout, overwrite)
                else:
                    fut = ex.submit(core.process_one, payload, outdir, want_original,
                                    want_pdf, cookie, self.timeout, overwrite, hint)
                futs[fut] = payload
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
                elif kind == "need_login":
                    self._scan_login_failed = True
                    self.log("登录态失效 —— 服务器返回的是登录页，已弹出获取说明", "err")
                    self._refresh_firefox_cookies()
                    self._update_auth_label()
                    self._show_login_help()
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
            extra = f"　({r['pagenum']} 页)" if r.get("pagenum") else ""
            self.log(f"✔ {r.get('filename')}{extra}", "ok")
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
    # 打包自检入口：超星文件下载器.exe --selftest [objectid ...] [--no-network]
    # 在出货的 exe 内部跑，验的就是真正要发出去的那个二进制。
    if "--selftest" in sys.argv:
        import selftest  # noqa: PLC0415

        oids = [a for a in sys.argv[1:] if not a.startswith("-")]
        sys.exit(0 if selftest.run(oids, network="--no-network" not in sys.argv) else 1)
    App().mainloop()


if __name__ == "__main__":
    main()
