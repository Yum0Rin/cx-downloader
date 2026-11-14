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
import tempfile
import urllib.error
import urllib.request
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from tkinter import colorchooser, filedialog, messagebox

import customtkinter as ctk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cx_download as core  # noqa: E402

SETTINGS = os.path.join(os.path.expanduser("~"), ".cx_download_gui.json")
QUEUE_FILE = os.path.join(os.path.expanduser("~"), ".cx_download_queue.json")
# 一键更新的结果由替换用的批处理写在这里（只有 ok/fail + 版本号，纯 ASCII）。
# 不写这里就没法告诉用户「其实没换成」——装在没有写权限的目录时会静默失败。
UPDATE_STATUS = os.path.join(tempfile.gettempdir(), "cx-self-update-status.txt")
QQ_GROUP = "1129676506"


class UpdatePaused(RuntimeError):
    """下载超时 / 被中止，但残包保留、可以续传。"""


def download_update_package(asset: str, tmp: str, total: int = 0,
                            proxy: str = "", progress=None,
                            budget: float = 300.0) -> int:
    """把更新包下到 tmp，支持断点续传，返回最终字节数。

    - `%TEMP%` 里同版本的残包直接复用，带 `Range: bytes=<done>-` 接着下；
    - 服务端忽略 Range（回 200）时退化成整包重下；
    - 416 视为「已经下到末尾」；
    - 单次尝试超过 `budget` 秒抛 UpdatePaused，**残包保留**，下次点「一键更新」接着下；
    - 两次尝试都失败抛 RuntimeError，残包同样保留。
    """
    if proxy and "://" not in proxy:
        proxy = "http://" + proxy
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        opener = urllib.request.build_opener()

    last: Exception | None = None
    for attempt in range(2):
        done = os.path.getsize(tmp) if os.path.exists(tmp) else 0
        if total and done >= total:
            break                       # 残包已经完整，直接进校验
        headers = {"User-Agent": core.UA}
        if done:
            headers["Range"] = f"bytes={done}-"
        try:
            req = urllib.request.Request(asset, headers=headers)
            started = time.time()
            with opener.open(req, timeout=120) as r:
                # 服务端忽略 Range（回 200）→ 只能从头写
                if done and getattr(r, "status", 200) == 200:
                    done = 0
                with open(tmp, "ab" if done else "wb") as f:
                    while True:
                        chunk = r.read(256 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
                        if time.time() - started > budget:
                            raise UpdatePaused(
                                "这次下载超过 5 分钟已暂停（已下 "
                                f"{core.human(done)}"
                                f"{' / ' + core.human(total) if total else ''}）。"
                                "残包已保留，再点一次「一键更新」会从断点接着下；"
                                "也可以改用「浏览器下载」")
            if total and done < total:
                # 连接被提前关掉时 read() 只返回 b""，不抛异常 —— 这里补一刀，
                # 让同一次点击里的第二次尝试接着下，而不是直接报「大小不符」
                raise RuntimeError(
                    f"连接提前结束（{core.human(done)} / {core.human(total)}）")
            break
        except UpdatePaused:
            raise
        except urllib.error.HTTPError as e:
            # 416：Range 起点已到末尾 → 残包其实就是完整文件
            if e.code == 416 and done and os.path.exists(tmp):
                break
            last = e
        except Exception as e:  # noqa: BLE001
            last = e
        if attempt == 0:
            time.sleep(1.0)
    else:
        raise RuntimeError(f"更新包下载失败：{last}（再点一次会从断点接着下）")
    return os.path.getsize(tmp) if os.path.exists(tmp) else 0


def asset_path(name: str) -> str:
    """打包后资源在 _MEIPASS 解包目录，源码运行时在仓库根的 assets/。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "assets", name)


def self_update_script(new_exe: str, old: str, pid: int, ver: str) -> str:
    """生成「等进程退出 → 覆盖 → 重启」的批处理。

    单独成函数是为了能被探针直接调用（不依赖 GUI 实例）。脚本里只有 ASCII，
    路径另外按 GBK 写盘 —— 所以调用方要先 `script.encode("gbk")` 试一下。
    复制失败不再静默：连续 10 次失败或大小对不上，都写 `fail` 进状态文件，
    让下次启动能告诉用户；成功则写 `ok` + 版本号。
    """
    return (
        "@echo off\r\n"
        "setlocal\r\n"
        f'set "PID={pid}"\r\n'
        f'set "NEW={new_exe}"\r\n'
        f'set "OLD={old}"\r\n'
        f'set "STATUS={UPDATE_STATUS}"\r\n'
        f'set "VER={ver}"\r\n'
        ":wait\r\n"
        'tasklist /FI "PID eq %PID%" 2>NUL | find "%PID%" >NUL\r\n'
        "if not errorlevel 1 (\r\n"
        "  timeout /t 1 /nobreak >NUL\r\n"
        "  goto wait\r\n"
        ")\r\n"
        f'attrib -R "%OLD%" >NUL 2>NUL\r\n'
        "set /a tries=0\r\n"
        ":copy\r\n"
        'copy /Y "%NEW%" "%OLD%" >NUL 2>NUL\r\n'
        "if not errorlevel 1 goto verify\r\n"
        "set /a tries+=1\r\n"
        "if %tries% geq 10 goto failed\r\n"
        "timeout /t 2 /nobreak >NUL\r\n"
        "goto copy\r\n"
        ":verify\r\n"
        'for %%A in ("%NEW%") do set "NS=%%~zA"\r\n'
        'for %%A in ("%OLD%") do set "OS=%%~zA"\r\n'
        'if "%NS%"=="%OS%" goto ok\r\n'
        "goto failed\r\n"
        ":ok\r\n"
        '> "%STATUS%" echo ok\r\n'
        '>> "%STATUS%" echo %VER%\r\n'
        'start "" "%OLD%"\r\n'
        "goto quit\r\n"
        ":failed\r\n"
        '> "%STATUS%" echo fail\r\n'
        '>> "%STATUS%" echo %VER%\r\n'
        'start "" "%OLD%"\r\n'
        "goto quit\r\n"
        ":quit\r\n"
        '(goto) 2>nul & del "%~f0"\r\n'
    )
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


DEFAULT_ACCENT = "#2F4858"
ACCENT_PRESETS = [
    ("黛蓝", "#2F4858"),
    ("靛青", "#3B5BA5"),
    ("松绿", "#2E7D6B"),
    ("紫棠", "#6B4C8A"),
    ("朱砂", "#C03A2B"),
    ("玄灰", "#4A4F57"),
]


def _hex_rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _mix(color: str, other: str, t: float) -> str:
    """把两个颜色按比例混合（t=0 全取 color，t=1 全取 other）。"""
    a, b = _hex_rgb(color), _hex_rgb(other)
    return "#" + "".join(
        f"{round(x + (y - x) * t):02X}" for x, y in zip(a, b)
    )


def apply_accent(accent: str) -> None:
    """用选定的主题色重算明暗两套 primary / hover。

    浅色模式直接用选定色，hover 压暗；深色模式整体提亮一档，避免在深底上发闷。
    """
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", accent or ""):
        accent = DEFAULT_ACCENT
    LIGHT["primary"] = accent
    LIGHT["primary_hover"] = _mix(accent, "#000000", 0.16)
    DARK["primary"] = _mix(accent, "#FFFFFF", 0.16)
    DARK["primary_hover"] = _mix(accent, "#FFFFFF", 0.28)



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
MONO = "Consolas"

# 各平台常见字体，按优先级挑一个系统里真有的（Tk 会静默回退，但换个中文字体
# 观感差很多）。启动拿到 Tk root 后再定。
FONT_CANDIDATES = (
    "Microsoft YaHei UI", "Microsoft YaHei",           # Windows
    "PingFang SC", "Hiragino Sans GB",                 # macOS
    "Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Micro Hei",  # Linux
    "Noto Sans SC", "DejaVu Sans",
)
MONO_CANDIDATES = (
    "Consolas", "Cascadia Mono",                       # Windows
    "Menlo", "Monaco",                                 # macOS
    "DejaVu Sans Mono", "Noto Sans Mono CJK SC", "Liberation Mono", "Courier",
)


def pick_font(candidates: tuple[str, ...], fallback: str) -> str:
    """从 candidates 里挑第一个系统真装了的字体，都没有就返回 fallback。"""
    try:
        import tkinter.font as tkfont  # noqa: PLC0415

        have = {f.lower() for f in tkfont.families()}
    except Exception:  # noqa: BLE001
        return fallback
    for name in candidates:
        if name.lower() in have:
            return name
    return fallback


def open_folder(path: str) -> None:
    """在系统文件管理器里打开目录（Windows / macOS / Linux 三套命令）。"""
    if sys.platform.startswith("win"):
        os.startfile(path)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])

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
    "装了 Firefox 的话不用管这个 —— 工具会自动读它的登录态。\n\n"
    "手动 Cookie 优先于 Firefox。浏览器里换了账号，就点「换号」清掉它再扫；\n"
    "下面那行会显示当前账号姓名，扫出来的东西不对时先看是不是号不对。"
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
CACHE_HINT = (
    "记住上次扫到的文件列表，10 分钟内重复扫描同一门课会直接秒开。\n\n"
    "想拿最新列表：再点一次「扫描」就会跳过缓存重新扫。\n"
    "关掉的话，每次扫描都向服务器重新要一遍（24 章约 10 秒）。"
)
FALLBACK_HINT = (
    "只在「仅 PDF」模式下起作用。\n\n"
    "csv / py / ipynb / npy / xlsx 这类格式学习通不转 PDF，\n"
    "「仅 PDF」模式本来一个都下不到。开了这个开关，这些文件改下原件，\n"
    "能转 PDF 的照旧只给 PDF。\n\n"
    "关掉的话，它们会被标成「跳过：该格式没有 PDF 版」。"
)
VIDEO_HINT = (
    "视频任务点默认跳过：它没有 PDF，也不走文档那套接口。\n\n"
    "打开后可以选两种取法：\n\n"
    "转码 mp4 —— 学习通服务端压好的版本，几十 MB，\n"
    "  普通播放器直接能放；画质是被压过的（不是原件）。\n"
    "  实测同一门课：原件 809.7MB → 转码 33.8MB，约 1/24。\n\n"
    "原件 —— 老师上传的那份（.mov/.mp4），画质码率最好，\n"
    "  但体积大得多（几百 MB 到 1GB），.mov 可能还要装解码器。\n\n"
    "没有转码版的视频会自动退回原件。视频体积大，建议单独下一批。\n"
    "CLI：--video（加 --video-original 取原件）。"
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
# 视频两种取法：转码 mp4 是学习通压过的（几十 MB），原件是老师上传的那份（几百 MB）
VIDEO_MODES = ("转码 mp4", "原件")
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


def course_path(base: str, subdir: str, group: str, unit: str, chapter: str,
                lv: tuple[bool, bool, bool, bool]) -> str:
    """按层级勾选拼落点目录（课程名 / 来源 / 分组 / 章节名）。

    单独拎出来是为了让扫描线程算查重路径时也用同一套规则 —— 之前扫描只按课程目录找，
    文件实际落在「分组/章节」子目录里，于是明明下过也全标「新文件」。
    """
    parts: list[str] = []
    if lv[0] and subdir:
        parts.append(subdir)
    if lv[1] and group:
        parts.append(core.sanitize(group, "其他"))
    if lv[2] and unit:
        parts.append(core.sanitize(unit, "分组"))
    if lv[3] and chapter:
        # 「资料」的 chapter 可能是多级文件夹路径（"a / b"），逐级建
        for seg in str(chapter).split(" / "):
            seg = core.sanitize(seg, "")
            if seg:
                parts.append(seg)
    return os.path.join(base, *parts) if parts else base


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

    def _pop(self, evt=None):
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
        # 指针和 Toplevel geometry 都是物理像素；winfo_screen* 在缩放环境下
        # 可能是逻辑值，所以屏幕边界直接用系统物理分辨率。
        try:
            mx, my = self.winfo_pointerx(), self.winfo_pointery()
        except Exception:  # noqa: BLE001
            mx = self.winfo_rootx() + self.winfo_width()
            my = self.winfo_rooty()
        try:
            sw = ctypes.windll.user32.GetSystemMetrics(0)
            sh = ctypes.windll.user32.GetSystemMetrics(1)
        except Exception:  # noqa: BLE001
            sw, sh = tip.winfo_screenwidth(), tip.winfo_screenheight()
        gap, pad = px(12), px(8)
        x, y = mx + gap, my + gap
        if x + w > sw - pad:
            x = mx - w - gap
        if y + h > sh - pad:
            y = my - h - gap
        x = max(pad, min(x, sw - w - pad))
        y = max(pad, min(y, sh - h - pad))
        tip.geometry(f"+{int(x)}+{int(y)}")
        self._tip = tip

    def _drop(self, _evt=None):
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:  # noqa: BLE001
                pass
            self._tip = None


class CanvasCourseList(ctk.CTkFrame):
    """课程选择列表：同样只画可见行，避免 60 门课一次建几百个控件。"""

    def __init__(self, master):
        super().__init__(master, fg_color=pair("input"), corner_radius=9,
                         border_width=1, border_color=pair("input_border"))
        self.items: list[dict] = []
        self.checked: set[int] = set()
        self._items: dict[int, list[int]] = {}
        self._drawn = (None, None)
        try:
            self.scale = ctk.ScalingTracker.get_widget_scaling(self) or 1.0
        except Exception:  # noqa: BLE001
            self.scale = 1.0
        self.row_h = max(24, int(30 * self.scale))
        self.px12 = max(9, int(12 * self.scale))
        self.px11 = max(8, int(11 * self.scale))
        self.font12 = tkfont.Font(family=FONT, size=-self.px12)
        self.font11 = tkfont.Font(family=FONT, size=-self.px11)
        dark = ctk.get_appearance_mode().lower() == "dark"
        self.canvas = tk.Canvas(self, bg=pair("input")[1 if dark else 0],
                                highlightthickness=0, bd=0,
                                yscrollincrement=self.row_h)
        self.vsb = ctk.CTkScrollbar(self, command=self._on_scroll)
        self.canvas.configure(yscrollcommand=self._on_yscroll)
        self.vsb.pack(side="right", fill="y", padx=(0, 6), pady=6)
        self.canvas.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        self.canvas.bind("<Configure>", lambda _e: self._layout())
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Button-1>", self._click)

    @staticmethod
    def _ti() -> int:
        return 1 if ctk.get_appearance_mode().lower() == "dark" else 0

    def _on_scroll(self, *args):
        self.canvas.yview(*args)
        self._redraw()

    def _on_yscroll(self, first, last):
        self.vsb.set(first, last)
        self._redraw()

    def _wheel(self, e):
        if e.delta:
            self.canvas.yview_scroll(int(-e.delta / 120) * 3, "units")
        return "break"

    def set_courses(self, items: list[dict], checked: set | None = None):
        self.items = items
        # checked 用课程 key，筛选只改显示的子集，已勾的隐藏课程不丢
        if checked is None:
            self.checked = {it["key"] for it in items
                            if it.get("status") == "进行中"}
        else:
            self.checked = checked
        self._clear()
        self._layout()
        self._redraw(force=True)

    def set_all(self, flag: bool):
        keys = {it["key"] for it in self.items}
        if flag:
            self.checked.update(keys)
        else:
            self.checked.difference_update(keys)
        self._redraw(force=True)

    def only_active(self):
        self.checked.clear()
        self.checked.update(it["key"] for it in self.items
                            if it.get("status") == "进行中")
        self._redraw(force=True)

    def selected(self) -> list[dict]:
        return [it for it in self.items if it["key"] in self.checked]

    def _clear(self):
        for ids in self._items.values():
            for it in ids:
                self.canvas.delete(it)
        self._items.clear()
        self._drawn = (None, None)

    def _layout(self):
        w = max(1, self.canvas.winfo_width())
        h = max(1, len(self.items) * self.row_h)
        self.canvas.configure(scrollregion=(0, 0, w, h))
        self._redraw(force=True)

    @staticmethod
    def _fit_text(text: str, font, max_px: int) -> str:
        if max_px <= 0:
            return ""
        if font.measure(text) <= max_px:
            return text
        s, ell = text, "…"
        while s and font.measure(s + ell) > max_px:
            s = s[:-1]
        return (s + ell) if s else ell

    def _draw_check(self, x, cy, on) -> list[int]:
        ti = self._ti()
        s = max(14, int(18 * self.scale))
        r = max(3, int(4 * self.scale))
        ids = [round_rect(
            self.canvas, x, cy - s // 2, x + s, cy + s // 2, r,
            fill=pair("primary")[ti] if on else pair("input")[ti],
            outline=pair("input_border")[ti], width=max(1, int(self.scale)))]
        if on:
            ids.append(self.canvas.create_line(
                x + s * 0.22, cy, x + s * 0.45, cy + s * 0.25,
                x + s * 0.8, cy - s * 0.25, fill="#FFFFFF",
                width=max(1, int(2 * self.scale)), capstyle="round"))
        return ids

    def _draw_row(self, idx: int, w: int) -> list[int]:
        it = self.items[idx]
        ti = self._ti()
        y0 = idx * self.row_h
        cy = y0 + self.row_h // 2
        ids = self._draw_check(int(18 * self.scale), cy,
                               it["key"] in self.checked)
        status = it.get("status") or "未知"
        color = {"进行中": pair("ok")[ti], "未开始": pair("primary")[ti]}.get(
            status, pair("muted")[ti])
        status_x = w - int(360 * self.scale)
        span_x = w - int(12 * self.scale)
        name_x = int(52 * self.scale)
        name = self._fit_text(it.get("name") or "",
                              self.font12, max(10, status_x - name_x - 8))
        ids.append(self.canvas.create_text(
            name_x, cy, anchor="w", text=name,
            font=self.font12, fill=pair("text")[ti]))
        ids.append(self.canvas.create_text(
            status_x, cy, anchor="w", text=status,
            font=self.font11, fill=color))
        span = self._fit_text(it.get("span") or "",
                              self.font11, max(10, span_x - status_x - 64))
        ids.append(self.canvas.create_text(
            span_x, cy, anchor="e", text=span,
            font=self.font11, fill=pair("muted")[ti]))
        return ids

    def _redraw(self, force: bool = False):
        top = self.canvas.canvasy(0)
        view_h = max(1, self.canvas.winfo_height())
        start = max(0, int(top // self.row_h) - 1)
        end = min(len(self.items), start + int(view_h // self.row_h) + 3)
        if force:
            for idx in list(self._items):
                for it in self._items.pop(idx):
                    self.canvas.delete(it)
        elif (start, end) == self._drawn:
            return
        self._drawn = (start, end)
        for idx in list(self._items):
            if idx < start or idx >= end:
                for it in self._items.pop(idx):
                    self.canvas.delete(it)
        w = max(1, self.canvas.winfo_width())
        for idx in range(start, end):
            if idx not in self._items:
                self._items[idx] = self._draw_row(idx, w)

    def _click(self, event):
        idx = int(self.canvas.canvasy(event.y) // self.row_h)
        if idx < 0 or idx >= len(self.items):
            return
        key = self.items[idx]["key"]
        if key in self.checked:
            self.checked.discard(key)
        else:
            self.checked.add(key)
        for it in self._items.pop(idx, []):
            self.canvas.delete(it)
        self._redraw(force=False)
        return "break"


class CanvasTaskList(ctk.CTkFrame):
    """单 Canvas 只画可见行：滚动只移动画布，不为每行建控件。

    这是 tksheet 等成熟 Tk 大表格的做法：数据在内存里，画布上只保留
    视口附近几十行的图形项，滚动时增删边缘行、公共行不动。
    """

    def __init__(self, master, app):
        super().__init__(master, fg_color=pair("input"), corner_radius=9,
                         border_width=1, border_color=pair("input_border"))
        self.app = app
        self.rows: list[tuple[str, object]] = []
        self._items: dict[int, list[int]] = {}
        self._drawn = (None, None)
        self._empty_id = None
        try:
            self.scale = ctk.ScalingTracker.get_widget_scaling(self) or 1.0
        except Exception:  # noqa: BLE001
            self.scale = 1.0
        self.row_h = max(24, int(30 * self.scale))
        self.px12 = max(9, int(12 * self.scale))
        self.px11 = max(8, int(11 * self.scale))
        self.font12 = tkfont.Font(family=FONT, size=-self.px12)
        self.font11 = tkfont.Font(family=FONT, size=-self.px11)
        self.font12b = tkfont.Font(family=FONT, size=-self.px12, weight="bold")
        dark = ctk.get_appearance_mode().lower() == "dark"
        self.canvas = tk.Canvas(self, bg=pair("input")[1 if dark else 0],
                                highlightthickness=0, bd=0,
                                yscrollincrement=self.row_h)
        self.vsb = ctk.CTkScrollbar(self, command=self._on_scroll)
        self.canvas.configure(yscrollcommand=self._on_yscroll)
        self.vsb.pack(side="right", fill="y", padx=(0, 6), pady=6)
        self.canvas.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        self.canvas.bind("<Configure>", lambda _e: self._layout())
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Button-1>", self._click)

    @staticmethod
    def _ti() -> int:
        return 1 if ctk.get_appearance_mode().lower() == "dark" else 0

    def _on_scroll(self, *args):
        self.canvas.yview(*args)
        self._redraw()

    def _on_yscroll(self, first, last):
        self.vsb.set(first, last)
        self._redraw()

    def _wheel(self, e):
        if e.delta:
            self.canvas.yview_scroll(int(-e.delta / 120) * 3, "units")
        return "break"

    def set_rows(self, rows, keep_scroll: bool = False):
        first = self.canvas.yview()[0] if keep_scroll else 0.0
        self.rows = rows
        self._clear_items()
        self._layout()
        self.canvas.yview_moveto(first)
        self._redraw(force=True)

    def _layout(self):
        w = max(1, self.canvas.winfo_width())
        h = max(1, len(self.rows) * self.row_h)
        self.canvas.configure(scrollregion=(0, 0, w, h))
        self._redraw(force=True)

    def _clear_items(self):
        for items in self._items.values():
            for it in items:
                self.canvas.delete(it)
        self._items.clear()
        self._drawn = (None, None)
        if self._empty_id:
            self.canvas.delete(self._empty_id)
            self._empty_id = None

    def _draw_check(self, x, cy, on) -> list[int]:
        ti = self._ti()
        s = max(14, int(18 * self.scale))
        r = max(3, int(4 * self.scale))
        ids = [round_rect(
            self.canvas, x, cy - s // 2, x + s, cy + s // 2, r,
            fill=pair("primary")[ti] if on else pair("input")[ti],
            outline=pair("input_border")[ti], width=max(1, int(self.scale)))]
        if on:
            ids.append(self.canvas.create_line(
                x + s * 0.22, cy, x + s * 0.45, cy + s * 0.25,
                x + s * 0.8, cy - s * 0.25, fill="#FFFFFF",
                width=max(1, int(2 * self.scale)), capstyle="round"))
        return ids

    @staticmethod
    def _fit_text(text: str, font, max_px: int) -> str:
        """按像素宽度截断，超长补省略号，别顶到右边的字号/状态列。"""
        if max_px <= 0:
            return ""
        if font.measure(text) <= max_px:
            return text
        ell = "…"
        s = text
        while s and font.measure(s + ell) > max_px:
            s = s[:-1]
        return (s + ell) if s else ell

    def _draw_row(self, idx: int, w: int) -> list[int]:
        kind, obj = self.rows[idx]
        ti = self._ti()
        y0 = idx * self.row_h
        cy = y0 + self.row_h // 2
        cbx = int(18 * self.scale) if kind == "group" else int(42 * self.scale)
        text_x = cbx + int(26 * self.scale)
        ids: list[int] = []
        if kind == "group":
            ids.append(round_rect(
                self.canvas, 8, y0 + 2, w - 8, y0 + self.row_h - 2,
                max(5, int(8 * self.scale)),
                fill=pair("secondary")[ti], outline=""))
            info = self.app.groups.get(obj, {})
            members = info.get("members") or []
            all_on = bool(members) and all(t["var"].get() for t in members)
            ids.extend(self._draw_check(cbx, cy, all_on))
            arrow = "▸" if obj in self.app.collapsed else "▾"
            tail = (f"　{info.get('count', len(members))} 个 · "
                    f"待下载 {info.get('will', 0)} · 约 "
                    f"{core.human(info.get('size', 0))}")
            max_w = (w - int(8 * self.scale)) - text_x
            head = self._fit_text(
                f"{arrow} {obj}", self.font12b,
                max(10, max_w - self.font12b.measure(tail)))
            ids.append(self.canvas.create_text(
                text_x, cy, anchor="w", text=head + tail,
                font=self.font12b, fill=pair("text")[ti]))
        else:
            t = obj
            ids.extend(self._draw_check(cbx, cy, t["var"].get()))
            size_x = w - int(96 * self.scale)
            label = self._fit_text(
                t["label"], self.font12,
                max(10, size_x - text_x - int(10 * self.scale)))
            ids.append(self.canvas.create_text(
                text_x, cy, anchor="w", text=label,
                font=self.font12, fill=pair("text")[ti]))
            ids.append(self.canvas.create_text(
                size_x, cy, anchor="e", text=t["size"],
                font=self.font11, fill=pair("muted")[ti]))
            if t["exists"]:
                state = "已有" if t["same"] else "有更新"
                color = (pair("ok")[ti] if t["same"]
                         else ("#B26A00" if ti == 0 else "#F0B35B"))
            else:
                state = "缺失"
                color = "#C03A2B" if ti == 0 else "#EE7B6E"
            ids.append(self.canvas.create_text(
                w - int(12 * self.scale), cy, anchor="e", text=state,
                font=self.font11, fill=color))
        return ids

    def _redraw(self, force: bool = False):
        if not self.rows:
            if force or self._items:
                self._clear_items()
            if self._empty_id is None:
                ti = self._ti()
                w = max(1, self.canvas.winfo_width())
                h = max(1, self.canvas.winfo_height())
                self._empty_id = self.canvas.create_text(
                    w // 2, h // 2,
                    text="还没有内容 —— 在上面粘好链接，点「扫描」把文件列出来",
                    font=self.font12, fill=pair("muted")[ti],
                    anchor="center")
            return
        if self._empty_id:
            self.canvas.delete(self._empty_id)
            self._empty_id = None
        top = self.canvas.canvasy(0)
        view_h = max(1, self.canvas.winfo_height())
        start = max(0, int(top // self.row_h) - 1)
        end = min(len(self.rows), start + int(view_h // self.row_h) + 3)
        if force:
            for idx in list(self._items):
                for it in self._items.pop(idx):
                    self.canvas.delete(it)
        elif (start, end) == self._drawn:
            return
        self._drawn = (start, end)
        for idx in list(self._items):
            if idx < start or idx >= end:
                for it in self._items.pop(idx):
                    self.canvas.delete(it)
        w = max(1, self.canvas.winfo_width())
        for idx in range(start, end):
            if idx in self._items:
                continue
            self._items[idx] = self._draw_row(idx, w)

    def refresh(self):
        self._redraw(force=True)

    def restyle(self):
        dark = ctk.get_appearance_mode().lower() == "dark"
        self.canvas.configure(bg=pair("input")[1 if dark else 0])
        self._redraw(force=True)

    def _click(self, event):
        cy = self.canvas.canvasy(event.y)
        idx = int(cy // self.row_h)
        if idx < 0 or idx >= len(self.rows):
            return
        x = self.canvas.canvasx(event.x)
        kind, obj = self.rows[idx]
        cbx = int(18 * self.scale) if kind == "group" else int(42 * self.scale)
        hit = cbx <= x <= cbx + int(18 * self.scale)
        if kind == "group":
            if hit:
                self.app._toggle_group_all(obj)
            else:
                self.app._toggle_group(obj)
        else:
            if hit:
                obj["var"].set(not obj["var"].get())
                self.app._update_sel()
        return "break"


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

        # 按当前系统装了的字体定界面字体（Tk root 建好之后才能查）
        global FONT, MONO
        FONT = pick_font(FONT_CANDIDATES, FONT)
        MONO = pick_font(MONO_CANDIDATES, MONO)

        self.title("超星 / 学习通 文件批量下载器")
        self.configure(fg_color=pair("page"))
        self._fit_geometry(1120, 830)
        # 再小就会把底部操作栏 / 左栏卡片挤掉，定个下限
        self.minsize(900, 680)

        cfg = load_settings()
        self.accent = cfg.get("accent") or DEFAULT_ACCENT
        apply_accent(self.accent)
        self.outdir = ctk.StringVar(value=cfg.get("outdir") or DEFAULT_OUT)
        self.mode = ctk.StringVar(value=MODE_BY_VALUE.get(cfg.get("mode", "orig"), "仅原件"))
        # 「仅 PDF」模式下，csv/py/xlsx 这类服务端不转码的格式默认连原件一起下，
        # 否则它们只会被跳过、一个都拿不到。
        self.pdf_fallback = ctk.BooleanVar(value=bool(cfg.get("pdf_fallback", True)))
        # 视频任务点默认跳过（体积大、多数人只要课件）；开了就取转码 mp4
        self.video_dl = ctk.BooleanVar(value=bool(cfg.get("video", False)))
        self.video_mode = ctk.StringVar(
            value="原件" if cfg.get("video_original") else "转码 mp4")
        self.jobs = ctk.StringVar(value=str(cfg.get("jobs") or 4))
        self.auto_skip = ctk.BooleanVar(value=bool(cfg.get("auto_skip", True)))
        self.scan_cache = ctk.BooleanVar(value=bool(cfg.get("scan_cache", True)))
        self.cookie = ctk.StringVar(value=cfg.get("cookie") or "")
        # 可选：GitHub Release 资产下载代理，例如 http://127.0.0.1:7897
        self.upd_proxy = ctk.StringVar(value=cfg.get("update_proxy") or "")
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
        self._failed_jobs: list[tuple] = []   # 本次下载失败的 job，供「重试失败」
        self.groups: dict[str, dict] = {}     # 分组名 → {var, btn, members}
        self.collapsed: set[str] = set()      # 被折叠起来的分组名
        # 本次扫描时的「保存目录 + 层级勾选」，用来判断查重结果是否还有效
        self.scan_key = ""
        self._last_scan_sig = ""
        self._last_scan_ts = 0.0
        self._fx_cookie_count = 0             # 本机 Firefox 里 chaoxing cookie 的条数
        self._live_cookie = ""                # 从运行中 Firefox 读到的实时 Cookie
        self._account_name = ""               # 当前登录态对应的账号姓名（取不到就是空）
        self._account_pending = False         # 是否已有一个取账号名的后台请求在跑
        self._scan_login_failed = False       # 本次扫描是否因登录态中断
        self._checking_local = False          # 当前这轮是不是「扫描/检查」而不是普通下载
        self._scanning_all = False            # 当前这轮是不是「全部课程」
        self._all_reports: list[dict] = []    # 全部课程逐门汇总，最后出总结果
        self._special_pending: list[dict] = []  # 检查出的特殊/大文件，扫完统一问
        self.total = self.done = self.ok = self.fail = self.skip = 0
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
        self.after(300, self._probe_live_cookie)
        self.after(600, self._fetch_account_name_async)
        self.after(800, self._restore_queue)
        self.after(700, self._check_update_result)
        self.after(1500, self._check_update_async)
        self.after(100, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- 窗口 ----------------
    def _screen_size(self) -> tuple[int, int]:
        """屏幕物理分辨率。Windows 走 GetSystemMetrics（跟原来一致，避开 DPI 影响），
        其它平台用 Tk 自己的 winfo_screen*。"""
        if sys.platform.startswith("win"):
            try:
                return (
                    ctypes.windll.user32.GetSystemMetrics(0),
                    ctypes.windll.user32.GetSystemMetrics(1),
                )
            except Exception:  # noqa: BLE001
                pass
        return self.winfo_screenwidth(), self.winfo_screenheight()

    def _fit_geometry(self, want_w: int, want_h: int):
        """按真实物理分辨率 + CTk 缩放换算，保证窗口一定放得下。

        只设尺寸不够：窗口默认由系统随便摆，高一点的窗口会被摆到屏幕外，
        底部（操作行/日志）就看不见了。所以这里连位置一起算，居中并夹在屏幕内。
        """
        try:
            sw, sh = self._screen_size()
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
        """把系统标题栏染成与主题一致的颜色（Win11 22000+）。非 Windows 直接跳过。"""
        if not sys.platform.startswith("win"):
            return
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
        if getattr(self, "clist", None) is not None:
            try:
                self.clist.restyle()
            except Exception:  # noqa: BLE001
                pass
        # 选中态是靠变量算出来的，变量被外部改过时这里一并同步
        # 选项态是控件自绘的，跟随外部主题时得手动同步
        for tg in (getattr(self, "tg_appearance", None), getattr(self, "tg_mode", None)):
            if tg is None:
                continue
            try:
                tg._restyle()
            except Exception:  # noqa: BLE001
                pass

    # ---------------- 界面 ----------------
    def _build(self):
        self._build_header()
        # 先占住底部操作栏：窗口再往小挤，也先压缩中间内容，不会把按钮挤没
        self._build_actions(self)

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

    def _build_header(self):
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(12, 4))

        left = ctk.CTkFrame(head, fg_color="transparent")
        left.pack(side="left", fill="x", expand=True)
        title_row = ctk.CTkFrame(left, fg_color="transparent")
        title_row.pack(anchor="w")
        ctk.CTkLabel(
            title_row,
            text="超星文件下载器",
            font=ctk.CTkFont(FONT, 21, "bold"),
            text_color=pair("text"),
            anchor="w",
            height=28,
        ).pack(side="left")
        ctk.CTkLabel(
            title_row,
            text=f"v{core.__version__}",
            font=ctk.CTkFont(FONT, 11),
            text_color=pair("muted"),
            anchor="w",
        ).pack(side="left", padx=(8, 0), pady=(6, 0))
        ctk.CTkLabel(
            left,
            text="预览页链接 / objectid / 整门课程页链接 —— 课程页可一次扫出「章节任务点 + 资料页文件」",
            font=ctk.CTkFont(FONT, 12),
            text_color=pair("muted"),
            anchor="w",
            height=18,
        ).pack(anchor="w")

        self.btn_settings = self._btn(head, "设置", self._open_settings, width=68)
        self.btn_settings.pack(side="right", padx=(10, 0))

        # 有新版时才 pack（见 _on_update）
        self.btn_update = ctk.CTkButton(
            head, text="", width=118, height=32, corner_radius=9,
            font=ctk.CTkFont(FONT, 12), command=self._show_update,
            fg_color=pair("primary"), hover_color=pair("primary_hover"),
            text_color=("#FFFFFF", "#FFFFFF"),
        )
        self._update_info: dict | None = None

    # ---------------- GitHub / 更新 / 设置 ----------------
    def _open_github(self):
        try:
            webbrowser.open(core.GITHUB_URL)
        except Exception:  # noqa: BLE001
            pass

    def _copy_qq(self):
        try:
            self.clipboard_clear()
            self.clipboard_append(QQ_GROUP)
            self.log(f"已复制 QQ 群号：{QQ_GROUP}", "ok")
        except Exception:  # noqa: BLE001
            pass

    def _show_support(self):
        """设置页角落的「请作者喝杯奶茶」：点开并排显示微信 / 支付宝收款码。"""
        codes = [("微信", asset_path("support-wechat.png")),
                 ("支付宝", asset_path("support-alipay.png"))]
        items = [(label, p) for label, p in codes if os.path.isfile(p)]

        dlg = ctk.CTkToplevel(self)
        dlg.title("请作者喝杯奶茶")
        dlg.configure(fg_color=pair("page"))
        dlg.resizable(False, False)
        # 从设置弹窗里点开的：挂到设置窗口上，别被它压在下面
        parent = getattr(self, "_settings_dlg", None)
        if parent is not None and parent.winfo_exists():
            dlg.transient(parent)
        else:
            dlg.transient(self)
        try:
            dlg.lift()
            dlg.focus_force()
            dlg.attributes("-topmost", True)
            dlg.after(500, lambda: dlg.winfo_exists() and dlg.attributes("-topmost", False))
        except Exception:  # noqa: BLE001
            pass

        wrap = ctk.CTkFrame(dlg, fg_color=pair("card"), corner_radius=12,
                            border_width=1, border_color=pair("border"))
        wrap.pack(fill="both", padx=14, pady=14)
        ctk.CTkLabel(
            wrap, text="如果这个工具帮到了你，可以请作者喝杯奶茶", anchor="w",
            font=ctk.CTkFont(FONT, 14, "bold"), text_color=pair("primary"),
        ).pack(fill="x", padx=16, pady=(12, 2))

        if not items:
            ctk.CTkLabel(
                wrap, text="没找到收款码图片（assets/support-*.png）",
                font=ctk.CTkFont(FONT, 12), text_color=pair("muted"),
            ).pack(padx=16, pady=12)
        else:
            row = ctk.CTkFrame(wrap, fg_color="transparent")
            row.pack(pady=(6, 10))
            for label, p in items:
                col = ctk.CTkFrame(row, fg_color="transparent")
                col.pack(side="left", padx=10)
                img = tk.PhotoImage(file=p, master=dlg)
                if img.width() > 300:
                    f = max(1, round(img.width() / 300))
                    img = img.subsample(f)
                scale = ctk.ScalingTracker.get_window_scaling(self) or 1.0
                box = ctk.CTkLabel(
                    col, text="", image=img, fg_color="#FFFFFF",
                    corner_radius=10,
                    width=int(img.width() / scale) + 10,
                    height=int(img.height() / scale) + 10,
                )
                box.image = img        # 防止被 GC，图片会空白
                box.pack()
                ctk.CTkLabel(
                    col, text=label, font=ctk.CTkFont(FONT, 12, "bold"),
                    text_color=pair("text"),
                ).pack(pady=(6, 0))

        self._btn(wrap, "关闭", dlg.destroy, width=64).pack(pady=(0, 14))

        dlg.update_idletasks()
        scale = ctk.ScalingTracker.get_window_scaling(dlg) or 1.0
        w = int(wrap.winfo_reqwidth() / scale) + 28
        h = int(wrap.winfo_reqheight() / scale) + 28
        x = self.winfo_rootx() + (self.winfo_width() - w) // 2
        y = self.winfo_rooty() + (self.winfo_height() - h) // 3
        dlg.geometry(f"{w}x{h}+{max(20, x)}+{max(20, y)}")

    def _open_settings(self):
        dlg = ctk.CTkToplevel(self)
        dlg.title("设置")
        dlg.configure(fg_color=pair("card"))
        dlg.resizable(False, False)
        dlg.transient(self)
        try:
            dlg.after(60, dlg.lift)
        except Exception:  # noqa: BLE001
            pass
        self._settings_dlg = dlg

        # 不再套一层卡片：窗口本身就是背景，内容直接铺开
        wrap = ctk.CTkFrame(dlg, fg_color="transparent")
        wrap.pack(fill="both", expand=True)
        ctk.CTkLabel(
            wrap, text="设置", anchor="w", height=26,
            font=ctk.CTkFont(FONT, 15, "bold"), text_color=pair("primary"),
        ).pack(fill="x", padx=18, pady=(14, 6))

        # ---- 主题色 ----
        ctk.CTkLabel(wrap, text="主题色（改完保存，重启后生效）", anchor="w",
                     font=ctk.CTkFont(FONT, 12), text_color=pair("text"),
                     ).pack(fill="x", padx=16)
        self._accent_pick = self.accent
        swatches = ctk.CTkFrame(wrap, fg_color="transparent")
        swatches.pack(fill="x", padx=16, pady=(6, 0))
        accent_btns: dict[str, ctk.CTkButton] = {}
        lbl_accent = ctk.CTkLabel(
            wrap, text=f"当前：{self.accent}", anchor="w",
            font=ctk.CTkFont(FONT, 11), text_color=pair("muted"),
        )

        def mark_accent():
            for color, btn in accent_btns.items():
                if btn is None:
                    continue
                btn.configure(border_width=3 if color == self._accent_pick else 0,
                              border_color=pair("text"))
            lbl_accent.configure(text=f"当前：{self._accent_pick}")

        def choose(color):
            self._accent_pick = color
            mark_accent()

        for name, color in ACCENT_PRESETS:
            btn = ctk.CTkButton(
                swatches, text="", width=34, height=30, corner_radius=7,
                fg_color=color, hover_color=color,
                command=lambda c=color: choose(c),
            )
            btn.pack(side="left", padx=(0, 8))
            accent_btns[color] = btn

        def custom():
            _, hexv = colorchooser.askcolor(color=self._accent_pick,
                                            title="选择主题色")
            if hexv:
                choose(hexv.upper())

        self._btn(swatches, "自定义…", custom, width=84).pack(side="left")
        lbl_accent.pack(fill="x", padx=16, pady=(4, 0))
        mark_accent()

        # ---- 外观 ----
        ctk.CTkLabel(wrap, text="外观", anchor="w",
                     font=ctk.CTkFont(FONT, 12), text_color=pair("text"),
                     ).pack(fill="x", padx=16, pady=(12, 0))
        self.tg_appearance = ToggleGroup(
            wrap, values=["跟随系统", "浅色", "深色"],
            variable=self.appearance, command=self._on_appearance,
            selected=(pair("text"), pair("secondary_hover"), ("#FFFFFF", "#16191E")),
        )
        self.tg_appearance.pack(anchor="w", padx=16, pady=(4, 0))



        # ---- 关于 / 更新 ----
        ctk.CTkLabel(wrap, text="关于", anchor="w",
                     font=ctk.CTkFont(FONT, 12), text_color=pair("text"),
                     ).pack(fill="x", padx=16, pady=(14, 0))
        row_about = ctk.CTkFrame(wrap, fg_color="transparent")
        row_about.pack(fill="x", padx=16, pady=(4, 0))
        ctk.CTkLabel(row_about, text=f"超星文件下载器 v{core.__version__}",
                     font=ctk.CTkFont(FONT, 12), text_color=pair("text"),
                     anchor="w").pack(side="left")
        lbl_status = ctk.CTkLabel(row_about, text="", font=ctk.CTkFont(FONT, 11),
                                  text_color=pair("muted"), anchor="w")
        lbl_status.pack(side="left", padx=(10, 0))

        btn_check = self._btn(row_about, "检查更新", None, width=84)
        btn_check.pack(side="right")

        row_links = ctk.CTkFrame(wrap, fg_color="transparent")
        row_links.pack(fill="x", padx=16, pady=(8, 0))
        self._btn(row_links, "打开 GitHub", self._open_github, width=104).pack(side="left")
        self._btn(row_links, "最新 Release",
                  lambda: webbrowser.open(core.RELEASES_URL), width=104
                  ).pack(side="left", padx=(8, 0))
        if any(os.path.isfile(asset_path(f)) for f in
               ("support-wechat.png", "support-alipay.png")):
            self._btn(row_links, "请作者喝杯奶茶", self._show_support,
                      width=124).pack(side="left", padx=(8, 0))

        # 一键更新走 GitHub 资产 CDN，国内可能需要走代理
        row_proxy = ctk.CTkFrame(wrap, fg_color="transparent")
        row_proxy.pack(fill="x", padx=16, pady=(8, 0))
        ctk.CTkLabel(
            row_proxy, text="更新下载代理", anchor="w",
            font=ctk.CTkFont(FONT, 12), text_color=pair("muted"),
        ).pack(side="left")
        ctk.CTkEntry(
            row_proxy, textvariable=self.upd_proxy, height=30,
            corner_radius=8, border_width=1, border_color=pair("input_border"),
            fg_color=pair("input"), text_color=pair("text"),
            placeholder_text="可选，如 http://127.0.0.1:7897",
            placeholder_text_color=pair("muted"),
            font=ctk.CTkFont(FONT, 11),
        ).pack(side="left", fill="x", expand=True, padx=(8, 0))
        ctk.CTkLabel(
            wrap, text="国内下载 GitHub 慢或下不动时填本地代理（如 Clash 的 "
                       "127.0.0.1:7897）；留空会自动使用系统代理。",
            anchor="w", justify="left", wraplength=520,
            font=ctk.CTkFont(FONT, 11), text_color=pair("muted"),
        ).pack(fill="x", padx=16, pady=(2, 0))

        # 交流 / 反馈：一行字 + 一键复制群号
        row_qq = ctk.CTkFrame(wrap, fg_color="transparent")
        row_qq.pack(fill="x", padx=16, pady=(10, 0))
        ctk.CTkLabel(
            row_qq, text=f"想联系作者、反馈问题或交流使用，可以加 QQ 群 {QQ_GROUP}",
            anchor="w", font=ctk.CTkFont(FONT, 12), text_color=pair("muted"),
        ).pack(side="left")
        self._btn(row_qq, "复制群号", self._copy_qq, width=80).pack(side="left", padx=(10, 0))

        state = {"done": False, "info": None}

        def check():
            btn_check.configure(state="disabled", text="检查中…")
            lbl_status.configure(text="", text_color=pair("muted"))
            state["done"], state["info"] = False, None

            def work():
                try:
                    state["info"] = core.check_update()
                except Exception:  # noqa: BLE001
                    state["info"] = None
                state["done"] = True

            threading.Thread(target=work, daemon=True).start()

            def poll():
                if not dlg.winfo_exists():
                    return
                if not state["done"]:
                    dlg.after(200, poll)
                    return
                btn_check.configure(state="normal", text="检查更新")
                info = state["info"]
                if info:
                    lbl_status.configure(text=f"发现 v{info['version']}",
                                         text_color=pair("primary"))
                    btn_check.configure(text="查看更新",
                                        command=lambda: self._on_update(
                                            info, force_show=True))
                else:
                    lbl_status.configure(text="已是最新", text_color=pair("ok"))

            dlg.after(200, poll)

        btn_check.configure(command=check)

        # ---- 底部按钮 ----
        row = ctk.CTkFrame(wrap, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(16, 14))

        def save(restart=False):
            d = load_settings()
            changed = (d.get("accent") != self._accent_pick) or (
                d.get("appearance") != self.appearance.get())
            d["accent"] = self._accent_pick
            d["appearance"] = self.appearance.get()
            d["scan_cache"] = bool(self.scan_cache.get())
            d["update_proxy"] = self.upd_proxy.get().strip()
            save_settings(d)
            self.accent = self._accent_pick
            if restart:
                self._restart_app()
            elif changed:
                self.log("设置已保存。主题色需要重启程序才会生效。", "ok")
                messagebox.showinfo(
                    "设置已保存",
                    "主题色已保存，重启程序后生效。\n（外观切换是立即生效的）",
                )
            else:
                self.log("设置已保存", "dim")

        self._btn(row, "保存并重启", lambda: save(True), width=110).pack(side="right")
        self._btn(row, "保存", lambda: save(False), width=72).pack(side="right", padx=(0, 8))
        self._btn(row, "关闭", dlg.destroy, width=64).pack(side="left")

        dlg.update_idletasks()
        scale = ctk.ScalingTracker.get_window_scaling(dlg) or 1.0
        w = max(540, int(wrap.winfo_reqwidth() / scale))
        h = int(wrap.winfo_reqheight() / scale)
        x = self.winfo_rootx() + (self.winfo_width() - w) // 2
        y = self.winfo_rooty() + (self.winfo_height() - h) // 3
        dlg.geometry(f"{w}x{h}+{max(20, x)}+{max(20, y)}")

    @staticmethod
    def _restart_app():
        """按当前启动方式重启自己（源码版重启 python，打包版重启 exe）。"""
        if getattr(sys, "frozen", False):
            args = [sys.executable] + sys.argv[1:]
        else:
            args = [sys.executable] + sys.argv
        os.execv(sys.executable, args)

    def _check_update_async(self):
        def work():
            try:
                info = core.check_update()
            except Exception:  # noqa: BLE001
                info = None
            self.q.put(("update", info))

        threading.Thread(target=work, daemon=True).start()

    def _on_update(self, info, force_show: bool = False):
        if not info:
            return
        self._update_info = info
        self.btn_update.configure(text=f"更新 v{info['version']}")
        self.btn_update.pack(side="right", padx=(10, 0))
        self.log(f"发现新版本 v{info['version']}（当前 v{core.__version__}），"
                 f"点右上角「更新」查看改了什么", "head")
        seen = load_settings().get("seen_update") or ""
        # 启动时被忽略过的版本不再自动弹；设置里手动「查看更新」要强制弹
        if force_show or seen != info["version"]:
            self.after(400, self._show_update)

    @staticmethod
    def _plain_md(text: str, limit: int = 4000) -> str:
        """Release 正文是 Markdown，界面上只显示纯文本。"""
        t = text or ""
        t = re.sub(r"^#{1,6}\s*", "", t, flags=re.M)
        t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
        t = re.sub(r"`([^`]*)`", r"\1", t)
        t = re.sub(r"\[(.+?)\]\((.+?)\)", r"\1", t)
        t = re.sub(r"^\s*[-*]\s+", "· ", t, flags=re.M)
        t = re.sub(r"\n{3,}", "\n\n", t).strip()
        return t[:limit] + ("…" if len(t) > limit else "")

    def _show_update(self):
        info = getattr(self, "_update_info", None)
        if not info or not self._update_info:
            return
        dlg = ctk.CTkToplevel(self)
        dlg.title(f"发现新版本 v{info['version']}")
        dlg.configure(fg_color=pair("page"))
        dlg.resizable(False, False)
        dlg.transient(self)
        try:
            dlg.after(60, dlg.lift)
        except Exception:  # noqa: BLE001
            pass

        wrap = ctk.CTkFrame(dlg, fg_color=pair("card"), corner_radius=12,
                            border_width=1, border_color=pair("border"))
        wrap.pack(fill="both", padx=14, pady=14)
        ctk.CTkLabel(
            wrap, text=f"发现新版本 v{info['version']}", anchor="w", height=26,
            font=ctk.CTkFont(FONT, 15, "bold"), text_color=pair("primary"),
        ).pack(fill="x", padx=16, pady=(14, 2))
        ctk.CTkLabel(
            wrap, text=f"当前 v{core.__version__} → 最新 v{info['version']}",
            anchor="w", font=ctk.CTkFont(FONT, 12), text_color=pair("muted"),
        ).pack(fill="x", padx=16, pady=(0, 8))

        box = ctk.CTkTextbox(
            wrap, width=520, height=300, corner_radius=9, border_width=1,
            border_color=pair("input_border"), fg_color=pair("input"),
            text_color=pair("text"), font=ctk.CTkFont(FONT, 12), wrap="word",
        )
        box.pack(fill="both", expand=True, padx=16)
        box.insert("1.0", self._plain_md(info.get("body") or "（这个版本没写更新说明）"))
        box.configure(state="disabled")

        can_auto = (sys.platform.startswith("win")
                    and getattr(sys, "frozen", False)
                    and bool(info.get("asset_url")))
        self._upd_dlg = dlg
        self._upd_info = info

        row = ctk.CTkFrame(wrap, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(12, 4))
        if can_auto:
            self._upd_btn = self._btn(
                row, "一键更新", lambda: self._auto_update(info, dlg), width=110)
        else:
            self._upd_btn = self._btn(
                row, "下载新版本", lambda: webbrowser.open(info["url"]),
                width=110)
        self._upd_btn.pack(side="left")
        if can_auto:
            self._btn(row, "浏览器下载",
                      lambda: webbrowser.open(info["url"]),
                      width=96).pack(side="left", padx=(8, 0))
        self._btn(row, "稍后再说", dlg.destroy, width=88).pack(side="right")

        def ignore():
            d = load_settings()
            d["seen_update"] = info["version"]
            save_settings(d)
            self.log(f"已忽略 v{info['version']} 的更新提醒（可在设置里再检查）",
                     "dim")
            dlg.destroy()

        self._btn(row, "不再提示此版本", ignore, width=120).pack(
            side="right", padx=(0, 8))

        prog = ctk.CTkFrame(wrap, fg_color="transparent")
        prog.pack(fill="x", padx=16, pady=(0, 2))
        self._upd_lbl = ctk.CTkLabel(
            prog, text="", anchor="w", font=ctk.CTkFont(FONT, 11),
            text_color=pair("muted"),
        )
        self._upd_lbl.pack(fill="x")
        self._upd_pb = ctk.CTkProgressBar(
            prog, height=8, corner_radius=4,
            fg_color=pair("track"), progress_color=pair("primary"),
        )
        self._upd_pb.set(0)
        self._upd_pb.pack(fill="x", pady=(2, 0))

        if can_auto:
            hint = (
                f"一键更新：下载 v{info['version']} 的 Windows exe，退出后自动覆盖并重启。\n"
                "设置、登录态、下载队列都会保留，不用重新配置。\n"
                "装在 Program Files 等目录需要管理员权限，否则会提示替换失败；"
                "失败时新版本会留在临时目录，按提示手动复制即可。\n"
                "「稍后再说」下次启动仍会提醒；「不再提示此版本」只忽略这一个版本。")
        else:
            hint = (
                "浏览器下载后，用新的程序文件替换当前这份即可（设置会保留）。\n"
                "「稍后再说」下次启动仍会提醒；「不再提示此版本」只忽略这一个版本。\n"
                "忽略后仍可在「设置 → 关于 → 检查更新」里手动查看和更新。")
        # wraplength 按逻辑像素给就行：CTkLabel 内部会自己乘缩放
        ctk.CTkLabel(
            wrap, text=hint, anchor="w", justify="left", wraplength=520,
            font=ctk.CTkFont(FONT, 11), text_color=pair("muted"),
        ).pack(fill="x", padx=16, pady=(0, 14))

        dlg.update_idletasks()
        scale = ctk.ScalingTracker.get_window_scaling(dlg) or 1.0
        w = max(580, int(wrap.winfo_reqwidth() / scale) + 28)
        h = int(wrap.winfo_reqheight() / scale) + 28
        x = self.winfo_rootx() + (self.winfo_width() - w) // 2
        y = self.winfo_rooty() + (self.winfo_height() - h) // 3
        dlg.geometry(f"{w}x{h}+{max(20, x)}+{max(20, y)}")

    def _auto_update(self, info: dict, dlg):
        """下载 Release 里的 Windows exe，退出后由批处理覆盖自身并重启。"""
        if not (sys.platform.startswith("win") and getattr(sys, "frozen", False)):
            webbrowser.open(info.get("url") or core.RELEASES_URL)
            return
        asset = info.get("asset_url") or ""
        if not asset:
            webbrowser.open(info.get("url") or core.RELEASES_URL)
            return
        try:
            self._upd_btn.configure(state="disabled", text="下载中…")
            self._upd_lbl.configure(text="准备下载…", text_color=pair("muted"))
            self._upd_pb.set(0)
        except Exception:  # noqa: BLE001
            pass

        def work():
            try:
                tmp = os.path.join(
                    tempfile.gettempdir(),
                    f"cx-downloader-{info['version']}-new.exe")
                total = int(info.get("asset_size") or 0)
                proxy = (load_settings().get("update_proxy") or "").strip()
                # 已有残包时先把进度亮出来，让用户知道这次是接着下
                if os.path.exists(tmp):
                    self.q.put(("update_progress",
                                (os.path.getsize(tmp), total)))
                size = download_update_package(
                    asset, tmp, total, proxy,
                    progress=lambda got, tot: self.q.put(
                        ("update_progress", (got, tot))))
                if total and size != total:
                    raise RuntimeError(
                        f"下载大小不符：{size}/{total}（再点一次会接着下）")
                with open(tmp, "rb") as f:
                    if f.read(2) != b"MZ":
                        try:
                            os.remove(tmp)      # 内容不对，留着续传没意义
                        except OSError:
                            pass
                        raise RuntimeError("下载的文件不是有效的 Windows exe")
                self.q.put(("update_ready", (tmp, info)))
            except Exception as e:  # noqa: BLE001
                self.q.put(("update_failed", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _apply_update(self, new_exe: str, info: dict):
        """下载完的 exe 交给批处理：等本进程退出 → 覆盖 → 重启。

        成败都写进 UPDATE_STATUS（纯 ASCII），下次启动由 _check_update_result 读出来。
        之前这里连续复制失败 10 次也照样 `goto copied` 启动旧 exe，用户只看到
        「程序重启了」，版本没变且毫无提示 —— 装在没有写权限的目录（Program Files）
        或被杀软锁文件时必现。
        """
        dlg = getattr(self, "_upd_dlg", None)
        try:
            if dlg and dlg.winfo_exists():
                self._upd_pb.set(1.0)
                self._upd_lbl.configure(
                    text="下载完成，正在退出并替换…", text_color=pair("ok"))
        except Exception:  # noqa: BLE001
            pass

        old = sys.executable
        ver = str(info.get("version") or "")
        bat = os.path.join(
            tempfile.gettempdir(), f"cx-self-update-{int(time.time())}.bat")
        script = self_update_script(new_exe, old, os.getpid(), ver)
        try:
            data = script.encode("gbk")
        except UnicodeEncodeError:
            # 路径里有 GBK 编不出的字符，批处理会指向错误路径 —— 宁可不自动替换
            messagebox.showinfo(
                "需要手动更新",
                f"程序所在路径含 GBK 无法表示的字符，自动替换可能出错，已取消。\n\n"
                f"新版本已下载到：\n{new_exe}\n\n"
                f"请关闭本程序后，手动把它复制到：\n{old}",
                parent=self)
            webbrowser.open(info.get("url") or core.RELEASES_URL)
            return
        try:
            os.remove(UPDATE_STATUS)   # 清掉上一次的旧结果，免得读串
        except OSError:
            pass
        try:
            with open(bat, "wb") as f:
                f.write(data)
        except OSError as e:
            messagebox.showwarning(
                "无法自动更新",
                f"写替换脚本失败：{e}\n\n请用「浏览器下载」手动更新。",
                parent=self)
            return
        subprocess.Popen(
            ["cmd", "/c", bat],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            close_fds=True,
        )
        try:
            if dlg:
                dlg.destroy()
        except Exception:  # noqa: BLE001
            pass
        self.destroy()
        os._exit(0)

    def _check_update_result(self):
        """读上一次「一键更新」的替换结果（由批处理写入 UPDATE_STATUS）。

        成功：日志记一行，删掉 %TEMP% 里的残包。
        失败：弹窗说清楚 —— 新版本在哪、为什么可能失败、怎么手动完成。
        """
        try:
            if not os.path.exists(UPDATE_STATUS):
                return
            with open(UPDATE_STATUS, "r", encoding="utf-8", errors="replace") as f:
                lines = [ln.strip() for ln in f.read().splitlines() if ln.strip()]
            os.remove(UPDATE_STATUS)
        except OSError:
            return

        state = lines[0].lower() if lines else ""
        ver = lines[1] if len(lines) > 1 else ""
        new_exe = (os.path.join(tempfile.gettempdir(),
                                f"cx-downloader-{ver}-new.exe") if ver else "")
        # 批处理说成功，还要当前真的跑着新版本才算数
        replaced = bool(ver) and (core.parse_version(ver)
                                  <= core.parse_version(core.__version__))
        if state == "ok" and replaced:
            self.log(f"已更新到 v{ver}（自动替换完成）。", "head")
            if new_exe and os.path.exists(new_exe):
                try:
                    os.remove(new_exe)
                except OSError:
                    pass
            return

        old = sys.executable
        where = f"\n\n新版本已下载到：\n{new_exe}" if (new_exe and
                                                     os.path.exists(new_exe)) else ""
        self.log(f"自动更新没完成：v{ver or '?'} 未能替换当前程序。", "err")
        messagebox.showwarning(
            "自动更新没完成",
            f"新版本 v{ver or '?'} 没能替换掉当前程序，现在运行的还是 "
            f"v{core.__version__}。{where}\n\n"
            "常见原因：\n"
            "· 程序所在目录没有写权限（例如装在 Program Files）\n"
            "· 文件被杀毒软件 / 安全软件占用\n\n"
            "处理办法：关闭本程序，把上面那个 exe 复制到\n"
            f"{old}\n"
            "（覆盖原文件）再重新打开即可。也可以点「浏览器下载」重新下载。",
            parent=self)

    def _update_failed(self, msg: str):
        dlg = getattr(self, "_upd_dlg", None)
        if dlg and dlg.winfo_exists():
            try:
                self._upd_btn.configure(state="normal", text="一键更新")
                self._upd_lbl.configure(
                    text=f"更新失败：{msg}",
                    text_color=("#C03A2B", "#EE7B6E"))
            except Exception:  # noqa: BLE001
                pass
        messagebox.showwarning(
            "更新失败",
            f"{msg}\n\n可以点「下载新版本」手动更新。")

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
            font=ctk.CTkFont(MONO, 12),
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

        # 单独一行：塞进「下载内容」那行会被左栏宽度挤掉，只剩个「无」字
        row_fb = ctk.CTkFrame(card, fg_color="transparent")
        row_fb.pack(fill="x", padx=14, pady=(0, 6))
        ctk.CTkSwitch(
            row_fb,
            text="无 PDF 版时下原件",
            variable=self.pdf_fallback,
            font=ctk.CTkFont(FONT, 12),
            text_color=pair("text"),
            progress_color=pair("primary"),
            fg_color=pair("input_border"),
            button_color=("#FFFFFF", "#E4EAF0"),
            button_hover_color=("#FFFFFF", "#FFFFFF"),
        ).pack(side="left")
        HintIcon(row_fb, FALLBACK_HINT).pack(side="left", padx=(6, 12))

        # 视频单独一行：开关 + 取法下拉，挤在上一行会被裁掉
        row_vid = ctk.CTkFrame(card, fg_color="transparent")
        row_vid.pack(fill="x", padx=14, pady=(0, 6))
        ctk.CTkSwitch(
            row_vid,
            text="视频也下载",
            variable=self.video_dl,
            command=self._on_video_toggle,
            font=ctk.CTkFont(FONT, 12),
            text_color=pair("text"),
            progress_color=pair("primary"),
            fg_color=pair("input_border"),
            button_color=("#FFFFFF", "#E4EAF0"),
            button_hover_color=("#FFFFFF", "#FFFFFF"),
        ).pack(side="left")
        HintIcon(row_vid, VIDEO_HINT).pack(side="left", padx=(6, 8))
        # 视频要哪种：转码 mp4 体积小、能直接播；原件是老师传的那份，画质最好但大得多
        self.om_video = ctk.CTkOptionMenu(
            row_vid,
            values=list(VIDEO_MODES),
            variable=self.video_mode,
            width=96,
            height=28,
            corner_radius=9,
            font=ctk.CTkFont(FONT, 12),
            fg_color=pair("secondary"),
            button_color=pair("secondary_hover"),
            button_hover_color=pair("border"),
            text_color=pair("text"),
            dropdown_fg_color=pair("card"),
            dropdown_hover_color=pair("secondary"),
            dropdown_text_color=pair("text"),
            state="normal" if self.video_dl.get() else "disabled",
        )
        self.om_video.pack(side="left")

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

        row_cache = ctk.CTkFrame(card, fg_color="transparent")
        row_cache.pack(fill="x", padx=14, pady=(0, 2))
        ctk.CTkSwitch(
            row_cache,
            text="扫描结果缓存",
            variable=self.scan_cache,
            font=ctk.CTkFont(FONT, 12),
            text_color=pair("text"),
            progress_color=pair("primary"),
            fg_color=pair("input_border"),
            button_color=("#FFFFFF", "#E4EAF0"),
            button_hover_color=("#FFFFFF", "#FFFFFF"),
        ).pack(side="left")
        HintIcon(row_cache, CACHE_HINT).pack(side="left", padx=(6, 0))

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
        self._btn(row3, "换号", self.switch_account, width=56).pack(side="left", padx=(6, 0))

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

        self.clist = CanvasTaskList(card, self)
        self.clist.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        self.groups = {}
        self.collapsed = set()
        self._auto_collapse_done = False

    def _clear_tasks(self):
        self.tasks = []
        self.groups = {}
        self.collapsed = set()
        self._auto_collapse_done = False
        if hasattr(self, "clist"):
            self.clist.set_rows([])
        self._update_sel()
        self._update_preview()

    def _build_rows(self, apply_default: bool = True):
        by_group: dict[str, list[dict]] = {}
        order: list[str] = []
        for t in self.tasks:
            grp = t["group"]
            if grp not in by_group:
                by_group[grp] = []
                order.append(grp)
            by_group[grp].append(t)
        # 一次画上千行也不行；大列表默认折叠，展开某组才把它的行放进可见列表
        if (apply_default and not self._auto_collapse_done
                and len(self.tasks) > 200):
            self.collapsed = set(order)
            self._auto_collapse_done = True
        self.groups = {}
        rows: list[tuple[str, object]] = []
        for grp in order:
            members = by_group[grp]
            will = [t for t in members if self._will_download(t)]
            self.groups[grp] = {
                "members": members,
                "count": len(members),
                "will": len(will),
                "size": sum(t["bytes"] for t in will),
            }
            rows.append(("group", grp))
            if grp not in self.collapsed:
                rows.extend(("task", t) for t in members)
        return rows

    def _render_tasks(self, apply_default: bool = True):
        if not hasattr(self, "clist"):
            return
        if not self.tasks:
            self.groups = {}
            self.clist.set_rows([])
            self._update_sel()
            self._update_preview()
            return
        self.clist.set_rows(self._build_rows(apply_default))
        self._update_sel()
        self._update_preview()

    def _toggle_group(self, grp: str):
        if grp in self.collapsed:
            self.collapsed.discard(grp)
        else:
            self.collapsed.add(grp)
        self.clist.set_rows(self._build_rows(apply_default=False), keep_scroll=True)
        self._update_sel()

    def _toggle_group_all(self, grp: str):
        """组级勾选：全选 / 全不选这一组。"""
        members = [t for t in self.tasks if t["group"] == grp]
        target = not all(t["var"].get() for t in members)
        for t in members:
            t["var"].set(target)
        self._update_sel()

    def _will_download(self, t: dict) -> bool:
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
        for info in self.groups.values():
            members = info["members"]
            w2 = [t for t in members if self._will_download(t)]
            info["count"] = len(members)
            info["will"] = len(w2)
            info["size"] = sum(t["bytes"] for t in w2)
        if hasattr(self, "clist"):
            self.clist.refresh()
        self._update_count()
        if self.tasks:
            if getattr(self, "_save_queue_after", None):
                try:
                    self.after_cancel(self._save_queue_after)
                except Exception:  # noqa: BLE001
                    pass
            self._save_queue_after = self.after(800, self._save_queue)

    def _build_actions(self, parent):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(side="bottom", fill="x", pady=(10, 8))

        scan_box = ctk.CTkFrame(
            row, fg_color=pair("secondary"), corner_radius=11, height=40,
        )
        scan_box.pack(side="left")
        self.btn_scan = ctk.CTkButton(
            scan_box,
            text="扫描",
            width=78,
            height=36,
            corner_radius=9,
            font=ctk.CTkFont(FONT, 13, "bold"),
            fg_color=pair("secondary"),
            hover_color=pair("secondary_hover"),
            text_color=pair("text"),
            text_color_disabled=pair("muted"),
            command=self.scan,
        )
        self.btn_scan.pack(side="left", padx=(4, 0), pady=2)
        HintIcon(
            scan_box,
            "扫描：检查输入框里的课程链接（递归查本地，按课程名找本地文件夹比对）。",
        ).pack(side="left", padx=(0, 6))

        # 「扫描全部课程」按账号列课程，勾选后批量检查，特殊/大文件单独问
        all_box = ctk.CTkFrame(
            row, fg_color=pair("secondary"), corner_radius=11, height=40,
        )
        all_box.pack(side="left", padx=(10, 0))
        self.btn_check = ctk.CTkButton(
            all_box,
            text="扫描全部课程",
            width=108,
            height=36,
            corner_radius=9,
            font=ctk.CTkFont(FONT, 13, "bold"),
            fg_color=pair("secondary"),
            hover_color=pair("secondary_hover"),
            text_color=pair("text"),
            text_color_disabled=pair("muted"),
            command=self.scan_all,
        )
        self.btn_check.pack(side="left", padx=(4, 0), pady=2)
        HintIcon(
            all_box,
            "扫描全部课程：列出账号全部课程（按开课时间排序、标进行中/未开始/已结束），\n"
            "勾选后逐门检查已有/有更新/缺失；特殊/大文件单独成组并询问。",
        ).pack(side="left", padx=(0, 6))

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

        self.btn_retry = ctk.CTkButton(
            row,
            text="重试失败",
            width=104,
            height=40,
            corner_radius=11,
            font=ctk.CTkFont(FONT, 13),
            fg_color=pair("primary"),
            hover_color=pair("primary_hover"),
            text_color="#FFFFFF",
            command=self._retry_failed,
        )

        self.pb = ctk.CTkProgressBar(
            row,
            height=8,
            corner_radius=4,
            fg_color=pair("track"),
            progress_color=pair("track"),
        )
        self.pb.set(0)
        # 空闲时不显示：平时只剩按钮和状态文字，不会多一条看不懂的空线

        self.lbl = ctk.CTkLabel(
            row, text="就绪", width=100, anchor="e",
            font=ctk.CTkFont(FONT, 13), text_color=pair("muted"), height=40,
        )
        self.lbl.pack(side="right", padx=(0, 12))

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
            font=ctk.CTkFont(MONO, 12),
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
        self._account_name = ""
        self._fetch_account_name_async()
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
        who = f" · 账号 {self._account_name}" if self._account_name else ""
        manual = self.cookie.get().strip()
        if manual:
            txt, color = f"登录态：手动 Cookie（{len(manual)} 字符）{who}", pair("ok")
        elif self._live_cookie:
            txt, color = f"登录态：Firefox 实时{who}", pair("ok")
        elif self._fx_cookie_count:
            txt = (f"登录态：本机 Firefox（{self._fx_cookie_count} 条 chaoxing cookie）{who}")
            color = pair("ok")
        else:
            txt = "登录态：无 —— 扫描会拿到登录页，点左边 ⓘ 看怎么弄"
            color = pair("primary")
        self.lbl_auth.configure(text=txt, text_color=color)

    def _probe_live_cookie(self):
        """启动时读一次运行中 Firefox 的实时 Cookie。

        学习通的登录票据经常只在浏览器内存里（磁盘 cookies.sqlite 里没有），
        只读磁盘会漏掉「现在登录的号」。读不到就静默回退。
        """
        if self.cookie.get().strip():
            return

        def work():
            try:
                groups = core.marionette_cookie_groups()
                cookie = ""
                for grp in groups:
                    if any(r["name"] in ("_uid", "UID") for r in grp):
                        cookie = core.cookie_header_from_rows(grp)
                        break
                self.q.put(("live_cookie", cookie))
            except Exception:  # noqa: BLE001
                self.q.put(("live_cookie", ""))

        threading.Thread(target=work, daemon=True).start()

    def _refresh_firefox_cookies(self):
        try:
            _, rows = core.load_firefox_cookies()
            self._fx_cookie_count = len(rows)
        except Exception:  # noqa: BLE001
            self._fx_cookie_count = 0

    def _fetch_account_name_async(self):
        """后台问一句「当前登录态是哪个号」，回来再更新标签。

        手动 Cookie 和 Firefox 登录态可能不是同一个账号，界面上不报名字就分不清，
        扫出来东西不对时很容易误判成「老师没传」。
        """
        cookie, _ = self._resolve_cookie()
        if not cookie:
            self._account_name = ""
            self._update_auth_label()
            return
        if self._account_pending:
            return
        self._account_pending = True

        def work():
            try:
                name = core.fetch_account_name(cookie, 12)
            except Exception:  # noqa: BLE001
                name = ""
            self.q.put(("account", name))

        threading.Thread(target=work, daemon=True).start()

    def switch_account(self):
        """换号：列出 Firefox 里能读到的所有学习通账号，让用户挑一个。

        一个 Firefox profile 一般只保留一个号的登录态；多个号来自多个 profile
        或 Multi-Account Containers（按 originAttributes 分开）。
        """
        self._open_account_picker()

    def _open_account_picker(self):
        dlg = ctk.CTkToplevel(self)
        dlg.title("选择学习通账号")
        dlg.configure(fg_color=pair("page"))
        dlg.resizable(False, False)
        dlg.transient(self)
        try:
            dlg.after(60, dlg.lift)
        except Exception:  # noqa: BLE001
            pass
        self._acct_dlg = dlg

        wrap = ctk.CTkFrame(dlg, fg_color=pair("card"), corner_radius=12,
                            border_width=1, border_color=pair("border"))
        wrap.pack(fill="both", padx=14, pady=14)
        ctk.CTkLabel(
            wrap, text="选择学习通账号", anchor="w", height=26,
            font=ctk.CTkFont(FONT, 15, "bold"), text_color=pair("primary"),
        ).pack(fill="x", padx=16, pady=(14, 4))

        body = ctk.CTkFrame(wrap, fg_color="transparent")
        body.pack(fill="x", padx=16)
        self._acct_body = body
        self._acct_status = ctk.CTkLabel(
            body, text="正在读取 Firefox 里的学习通账号…", anchor="w", justify="left",
            font=ctk.CTkFont(FONT, 12), text_color=pair("muted"), wraplength=430,
        )
        self._acct_status.pack(fill="x", pady=(2, 8))

        def work():
            try:
                accts = core.firefox_accounts()
            except Exception:  # noqa: BLE001
                accts = []
            self.q.put(("accounts", accts))

        threading.Thread(target=work, daemon=True).start()

        row = ctk.CTkFrame(wrap, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(10, 14))
        self._btn(row, "手动粘贴 Cookie", lambda: self.paste_cookie(dlg),
                  width=132).pack(side="left")
        self._btn(row, "取消", dlg.destroy, width=64).pack(side="right")

        dlg.update_idletasks()
        scale = ctk.ScalingTracker.get_window_scaling(dlg) or 1.0
        w = max(480, int(wrap.winfo_reqwidth() / scale) + 28)
        h = int(wrap.winfo_reqheight() / scale) + 28
        x = self.winfo_rootx() + (self.winfo_width() - w) // 2
        y = self.winfo_rooty() + (self.winfo_height() - h) // 3
        dlg.geometry(f"{w}x{h}+{max(20, x)}+{max(20, y)}")

    def _fill_accounts(self, accts):
        dlg = getattr(self, "_acct_dlg", None)
        if not dlg or not dlg.winfo_exists():
            return
        body = self._acct_body
        for w in body.winfo_children():
            w.destroy()

        def use(header, label):
            self.cookie.set(header)
            self._account_name = ""
            if not header:
                self._live_cookie = ""
                self._probe_live_cookie()
            self._update_auth_label()
            self.log(f"已切换到：{label}", "ok")
            self._persist_cookie()
            self._fetch_account_name_async()
            dlg.destroy()

        if not accts:
            ctk.CTkLabel(
                body, text=("Firefox 里没读到可用的学习通账号。\n"
                            "如果浏览器里明明登录着，可能是 Cookie 还没写盘：\n"
                            "在 Firefox 里刷新一下学习通页面，或直接粘贴 Cookie。"),
                anchor="w", justify="left", font=ctk.CTkFont(FONT, 12),
                text_color=pair("muted"), wraplength=430,
            ).pack(fill="x")
            return

        for a in accts:
            name = a.get("name") or "未识别姓名"
            src = a.get("profile_name") or "Firefox"
            if a.get("ctx") and not a.get("live"):
                src += " / 容器"
            label = f"{name} · {src}"
            if a.get("uid"):
                label += f"（UID {a['uid']}）"
            self._btn(body, label, lambda a=a, label=label: use(a["cookie"], label),
                      width=430, anchor="w").pack(fill="x", pady=2)

        if not any(a.get("live") for a in accts) and sys.platform.startswith("win"):
            self._btn(body, "让 Firefox 开启实时读取（会重启浏览器）",
                      self._restart_firefox_for_live,
                      width=430, anchor="w").pack(fill="x", pady=(8, 2))

        self._btn(body, "用 Firefox 自动（cookie 最全的那个）",
                  lambda: use("", "Firefox 自动"), width=430,
                  anchor="w").pack(fill="x", pady=(6, 2))

    def _find_firefox_exe(self) -> str:
        cands = [
            os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                         "Mozilla Firefox", "firefox.exe"),
            os.path.join(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                         "Mozilla Firefox", "firefox.exe"),
        ]
        for c in cands:
            if os.path.isfile(c):
                return c
        return ""

    def _restart_firefox_for_live(self):
        """重启 Firefox 并打开 Marionette，好读到只在内存里的登录 Cookie。"""
        exe = self._find_firefox_exe()
        if not exe:
            messagebox.showinfo(
                "没找到 Firefox",
                "没找到 firefox.exe。可以手动这样启动它：\n"
                "firefox.exe --marionette --remote-debugging-port",
            )
            return
        if not messagebox.askokcancel(
            "需要重启 Firefox",
            "为了读到「当前登录的账号」，需要让 Firefox 打开调试端口。\n\n"
            "这会关闭并重开 Firefox：标签页一般会自动恢复，但未提交的输入可能丢失。\n"
            "继续吗？",
        ):
            return
        try:
            subprocess.run(["taskkill", "/IM", "firefox.exe"],
                           capture_output=True, timeout=20)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2.5)
        try:
            subprocess.Popen([exe, "--marionette", "--remote-debugging-port"])
        except Exception as e:  # noqa: BLE001
            self.log(f"启动 Firefox 失败：{e}", "err")
            return
        self.log("已重开 Firefox，正在读取登录态…", "dim")
        self.after(4500, self._reload_accounts)

    def _reload_accounts(self):
        dlg = getattr(self, "_acct_dlg", None)
        if dlg and dlg.winfo_exists():
            dlg.destroy()
        self._live_cookie = ""
        self._probe_live_cookie()
        self.switch_account()

    def _persist_cookie(self):
        """只把 Cookie 落盘，不碰界面上其它未保存的设置。"""
        d = load_settings()
        d["cookie"] = self.cookie.get()
        save_settings(d)

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
        open_folder(d)

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
        # 下载模式也进 key：「仅 PDF」落的是 .pdf，跟原件模式查重结果不能混用
        return (f"{os.path.abspath(self.outdir.get())}|{self._levels_on()}"
                f"|{MODE_LABELS.get(self.mode.get(), 'orig')}")

    def _job_dir(self, base: str, t: dict) -> str:
        """按勾选的层级拼出这一项的落点目录。勾中哪级才建哪级。"""
        return course_path(base, t.get("subdir"), t.get("group"),
                           t.get("unit"), t.get("chapter"), self._lv_flags())

    def _lv_flags(self) -> tuple[bool, bool, bool, bool]:
        """层级勾选（主线程取好再传进扫描线程，别在子线程碰 Tk 变量）。"""
        return (bool(self.lv_course.get()), bool(self.lv_source.get()),
                bool(self.lv_unit.get()), bool(self.lv_chapter.get()))

    def _on_video_toggle(self):
        """没开视频下载时，转码/原件那个下拉没意义，灰掉。"""
        if hasattr(self, "om_video"):
            self.om_video.configure(state="normal" if self.video_dl.get() else "disabled")

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
        """登录态：手动 Cookie 优先，其次读运行中 Firefox 的实时 Cookie，最后读磁盘。"""
        manual = self.cookie.get().strip()
        if manual:
            return manual, "手动 Cookie"
        live = getattr(self, "_live_cookie", "")
        if live:
            return live, "Firefox 实时"
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
        self._checking_local = False
        self._special_pending = []
        self.btn_scan.configure(state="disabled")
        self.btn_start.configure(state="disabled")
        self.lbl.configure(text="扫描中…", text_color=pair("muted"))
        self.log(f"\n开始扫描：{len(urls)} 个课程页 + {len(oids)} 个单独文件", "head")
        self.log(f"登录态: {src}", "dim")

        self.scan_worker = threading.Thread(
            target=self._run_scan,
            args=(urls, oids, cookie, base,
                  MODE_LABELS.get(self.mode.get(), "orig") == "pdf",
                  self._lv_flags()),
            daemon=True,
        )
        self.scan_worker.start()

    def scan_all(self):
        """一键扫描：先列出账号全部课程，让用户勾选，再逐门检查。"""
        if self.scan_worker and self.scan_worker.is_alive():
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "正在下载，等下载结束再扫描。")
            return
        cookie, src = self._resolve_cookie()
        if not cookie:
            self.log("登录态：无 —— 列出全部课程需要登录，已弹出获取说明", "err")
            self._update_auth_label()
            self._show_login_help()
            return
        self.btn_scan.configure(state="disabled")
        self.btn_check.configure(state="disabled")
        self.btn_start.configure(state="disabled")
        self.lbl.configure(text="读取课程列表…", text_color=pair("muted"))
        self.log("\n读取账号全部课程…", "head")
        self.log(f"登录态: {src}", "dim")
        self.scan_worker = threading.Thread(
            target=self._run_list_courses, args=(cookie,), daemon=True)
        self.scan_worker.start()

    def _run_list_courses(self, cookie):
        try:
            courses = core.fetch_course_list(cookie, self.timeout,
                                             include_history=True)
            self.q.put(("course_list", courses))
        except core.LoginRequired:
            self.q.put(("need_login", None))
            self.q.put(("scan_done", None))
        except Exception as e:  # noqa: BLE001
            self.q.put(("scan_log", (f"读取课程列表失败：{e}", "err")))
            self.q.put(("scan_done", None))

    def _on_course_list(self, courses):
        self.btn_scan.configure(state="normal")
        self.btn_check.configure(state="normal")
        self.btn_start.configure(state="normal")
        self.lbl.configure(text="就绪", text_color=pair("muted"))
        if not courses:
            messagebox.showinfo("没有课程", "账号里没读到课程，检查登录态后再试。")
            return
        self._open_course_picker(courses)

    def _open_course_picker(self, courses):
        """列出全部课程：状态 + 开课时间，按开课时间排序，勾选后检查。"""
        def sort_key(c):
            b = c.get("beginDate") or ""
            return (not bool(b), b)

        courses = sorted(courses, key=sort_key)
        dlg = ctk.CTkToplevel(self)
        dlg.title("选择要检查的课程")
        dlg.configure(fg_color=pair("page"))
        dlg.transient(self)
        try:
            dlg.after(60, dlg.lift)
        except Exception:  # noqa: BLE001
            pass

        wrap = ctk.CTkFrame(dlg, fg_color=pair("card"), corner_radius=12,
                            border_width=1, border_color=pair("border"))
        wrap.pack(fill="both", expand=True, padx=14, pady=14)
        ctk.CTkLabel(
            wrap, text=f"账号共 {len(courses)} 门课（按开课时间排序）", anchor="w",
            font=ctk.CTkFont(FONT, 15, "bold"), text_color=pair("primary"),
        ).pack(fill="x", padx=16, pady=(12, 2))
        ctk.CTkLabel(
            wrap, text="默认勾选「进行中」的课程；未开始/已结束的按需勾选。",
            anchor="w", font=ctk.CTkFont(FONT, 11), text_color=pair("muted"),
        ).pack(fill="x", padx=16, pady=(0, 6))

        all_items = []
        for c in courses:
            status = core.course_time_status(c.get("beginDate"), c.get("endDate"))
            span = f"{c.get('beginDate') or '—'} ~ {c.get('endDate') or '—'}"
            all_items.append({
                "key": f"{c.get('courseid')}_{c.get('clazzid')}",
                "name": c.get("name") or f"课程{c.get('courseid')}",
                "status": status, "span": span, "course": c,
            })
        checked = {it["key"] for it in all_items if it["status"] == "进行中"}

        filt = ctk.CTkFrame(wrap, fg_color="transparent")
        filt.pack(fill="x", padx=16, pady=(0, 2))

        def mk_entry():
            return ctk.CTkEntry(
                filt, width=96, height=28, corner_radius=8,
                border_width=1, border_color=pair("input_border"),
                fg_color=pair("input"), text_color=pair("text"),
                placeholder_text="YYYY-MM-DD",
                placeholder_text_color=pair("muted"),
                font=ctk.CTkFont(FONT, 11),
            )

        ctk.CTkLabel(filt, text="开课日期", font=ctk.CTkFont(FONT, 11),
                     text_color=pair("muted"), height=28).pack(side="left")
        ent_b1 = mk_entry(); ent_b1.pack(side="left", padx=(6, 2))
        ctk.CTkLabel(filt, text="~", font=ctk.CTkFont(FONT, 11),
                     text_color=pair("muted")).pack(side="left")
        ent_b2 = mk_entry(); ent_b2.pack(side="left", padx=(2, 10))

        self.end_filter_on = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            filt, text="结束日期", variable=self.end_filter_on,
            command=lambda: on_end_toggle(), width=80,
            font=ctk.CTkFont(FONT, 11), text_color=pair("text"),
            checkbox_width=16, checkbox_height=16, corner_radius=4,
            fg_color=pair("primary"), hover_color=pair("primary_hover"),
            border_color=pair("input_border"), checkmark_color="#FFFFFF",
        ).pack(side="left")
        ent_e1 = mk_entry(); ent_e1.pack(side="left", padx=(6, 2))
        ctk.CTkLabel(filt, text="~", font=ctk.CTkFont(FONT, 11),
                     text_color=pair("muted")).pack(side="left")
        ent_e2 = mk_entry(); ent_e2.pack(side="left", padx=(2, 10))
        ent_e1.configure(state="disabled"); ent_e2.configure(state="disabled")

        bar = ctk.CTkFrame(wrap, fg_color="transparent")
        bar.pack(fill="x", padx=16, pady=(0, 4))
        clist = CanvasCourseList(wrap)
        clist.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        clist.set_courses(all_items, checked)
        lbl_filter = ctk.CTkLabel(
            filt, text=f"显示 {len(all_items)}/{len(all_items)} 门", height=28,
            font=ctk.CTkFont(FONT, 10), text_color=pair("muted"),
        )
        lbl_filter.pack(side="right")

        def on_end_toggle():
            st = "normal" if self.end_filter_on.get() else "disabled"
            ent_e1.configure(state=st)
            ent_e2.configure(state=st)

        def valid_date(d):
            return not d or re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) is not None

        def apply_filter():
            b1, b2 = ent_b1.get().strip(), ent_b2.get().strip()
            e1, e2 = ent_e1.get().strip(), ent_e2.get().strip()
            if not all(valid_date(x) for x in (b1, b2, e1, e2)):
                messagebox.showwarning("日期格式", "日期请填 YYYY-MM-DD，或留空。")
                return
            use_end = self.end_filter_on.get()
            subset = []
            for it in all_items:
                c = it["course"]
                b = (c.get("beginDate") or "")[:10]
                e = (c.get("endDate") or "")[:10]
                if b1 and (not b or b < b1):
                    continue
                if b2 and (not b or b > b2):
                    continue
                if use_end:
                    if e1 and (not e or e < e1):
                        continue
                    if e2 and (not e or e > e2):
                        continue
                subset.append(it)
            clist.set_courses(subset, checked)
            lbl_filter.configure(text=f"显示 {len(subset)}/{len(all_items)} 门")

        def reset_filter():
            for en in (ent_b1, ent_b2, ent_e1, ent_e2):
                en.delete(0, "end")
            self.end_filter_on.set(False)
            on_end_toggle()
            clist.set_courses(all_items, checked)
            lbl_filter.configure(text=f"显示 {len(all_items)}/{len(all_items)} 门")

        self._btn(filt, "筛选", apply_filter, width=52).pack(side="left", padx=(6, 0))
        self._btn(filt, "重置", reset_filter, width=52).pack(side="left", padx=4)

        def set_all(flag):
            clist.set_all(bool(flag))

        def only_active():
            clist.only_active()

        self._btn(bar, "全选", lambda: set_all(True), width=62).pack(side="left")
        self._btn(bar, "全不选", lambda: set_all(False), width=72).pack(side="left", padx=6)
        self._btn(bar, "只选进行中", only_active, width=96).pack(side="left")

        foot = ctk.CTkFrame(wrap, fg_color="transparent")
        foot.pack(fill="x", padx=16, pady=(0, 12))
        self._btn(foot, "取消", dlg.destroy, width=64).pack(side="right")
        go = ctk.CTkButton(
            foot, text="开始检查", width=120, height=34, corner_radius=9,
            font=ctk.CTkFont(FONT, 12, "bold"),
            fg_color=pair("primary"), hover_color=pair("primary_hover"),
            text_color="#FFFFFF",
        )
        go.pack(side="right", padx=(0, 8))

        def start():
            # 只检查当前筛选后仍可见、且被勾选的课程
            chosen = [it["course"] for it in clist.items
                      if it["key"] in checked]
            if not chosen:
                messagebox.showwarning("没选课程", "至少勾选一门课。")
                return
            dlg.destroy()
            self._start_course_check(chosen)

        go.configure(command=start)

        w, h = 980, 720
        x = self.winfo_rootx() + (self.winfo_width() - w) // 2
        y = self.winfo_rooty() + max(20, (self.winfo_height() - h) // 3)
        dlg.geometry(f"{w}x{h}+{max(20, x)}+{max(20, y)}")

    def _start_course_check(self, courses):
        cookie, src = self._resolve_cookie()
        if not cookie:
            self.log("登录态：无 —— 检查课程需要登录，已弹出获取说明", "err")
            self._show_login_help()
            return
        base = os.path.abspath(self.outdir.get())
        self.scan_key = self._dir_key()
        self._clear_tasks()
        self._checking_local = True
        self._scanning_all = True
        self._all_reports = []
        self._special_pending = []
        self.btn_scan.configure(state="disabled")
        self.btn_check.configure(state="disabled")
        self.btn_start.configure(state="disabled")
        self.lbl.configure(text=f"检查 {len(courses)} 门课…", text_color=pair("muted"))
        self.log(f"\n开始检查 {len(courses)} 门课", "head")
        self.log(f"登录态: {src}", "dim")
        self.scan_worker = threading.Thread(
            target=self._run_scan_all, args=(courses, cookie, base), daemon=True)
        self.scan_worker.start()

    def _run_scan_all(self, courses, cookie, base):
        def say(msg, tag="dim"):
            self.q.put(("scan_log", (msg, tag)))

        try:
            for i, c in enumerate(courses, 1):
                name = c.get("name") or f"课程{c.get('courseid')}"
                say(f"[{i}/{len(courses)}] {name}", "head")
                url = ("https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/studentcourse?"
                       f"courseid={c.get('courseid')}&clazzid={c.get('clazzid')}"
                       f"&cpi={c.get('cpi')}&ut=s")
                try:
                    rep = core.check_local_course(
                        cookie, url, base, self.timeout, progress=lambda m: say(m))
                except core.LoginRequired:
                    self.q.put(("need_login", None))
                    continue
                except Exception as e:  # noqa: BLE001
                    say(f"  检查失败：{e}", "err")
                    continue
                s = rep["summary"]
                say(f"  已有 {s['same']} · 有更新 {s['updated']} · "
                    f"缺失 {s['missing']} · 特殊 {s['special']}", "ok")
                self._all_reports.append(rep)
                self._special_pending.extend(
                    [f for f in rep["files"] if f.get("special")])
                self.q.put(("scan_files", (
                    name, core.sanitize(name, c.get("courseid")), rep["files"])))
        except Exception as e:  # noqa: BLE001
            say(f"检查出错：{e}", "err")
        self.q.put(("scan_done", None))

    def _run_scan(self, urls, oids, cookie, base, pdf_only=False,
                  lv=(True, False, False, False)):
        def say(msg, tag="dim"):
            self.q.put(("scan_log", (msg, tag)))

        try:
            use_cache = bool(self.scan_cache.get())
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
                    rep = core.check_local_course(
                        cookie, u, base, self.timeout,
                        progress=lambda m: say(m), use_cache=use_cache)
                except core.LoginRequired:
                    # 手动 Cookie 经常是几天前粘的，早过期了；自动退到 Firefox 再试
                    fb = self._live_cookie or core.firefox_cookie_header()
                    if fb and fb != cookie:
                        say("手动 Cookie 失效，改用 Firefox 登录态重试…", "dim")
                        try:
                            rep = core.check_local_course(
                                fb, u, base, self.timeout,
                                progress=lambda m: say(m), use_cache=use_cache)
                        except core.LoginRequired:
                            self.q.put(("need_login", None))
                            continue
                        except Exception as e:  # noqa: BLE001
                            say(f"检查失败：{e}", "err")
                            continue
                    else:
                        self.q.put(("need_login", None))
                        continue
                except Exception as e:  # noqa: BLE001
                    say(f"检查失败：{e}", "err")
                    continue

                s = rep["summary"]
                say(f"课程：{rep['name']}", "ok")
                say(f"本地目录：{rep['local_dir'] or '（无）'}")
                say(f"已有 {s['same']} · 有更新 {s['updated']} · "
                    f"缺失 {s['missing']} · 特殊 {s['special']}", "ok")
                self._special_pending.extend(
                    [f for f in rep["files"] if f.get("special")])
                self.q.put(("scan_files", (
                    rep["name"], core.sanitize(rep["name"], rep["courseid"]),
                    rep["files"])))

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
            special = bool(f.get("special"))
            source = f.get("source") or "其他"
            prefix = elide(course, 24) if course else ""
            # 全部课程时同一门课会混在一起，分组头带上课程名
            group = (f"{prefix} · 特殊/大文件（默认不下载）" if special
                     else (f"{prefix} · {source}" if prefix else source))
            self.tasks.append({
                "kind": "data" if url else "oid",
                "payload": url or f.get("objectid") or "",
                "oid": f.get("objectid") or "",
                # 特殊文件单独成组、默认不勾，避免把动辄几百 MB 的视频/安装包混进来
                "group": group,
                "subdir": subdir,
                "unit": f.get("unit") or "",
                "chapter": f.get("chapter") or "",
                "scan_key": self.scan_key,
                "name": name,
                "label": label,
                "size": f.get("hsize") or core.human(int(f.get("length") or 0)),
                "bytes": int(f.get("length") or 0) or core.parse_hsize(f.get("hsize") or ""),
                "course": course,
                "path": f.get("path") or f.get("local_path") or "",
                "exists": exists,
                "same": same,
                "special": special,
                "special_reason": f.get("special_reason") or "",
                "var": ctk.BooleanVar(value=not special),
            })
        self._render_tasks()
        self._save_queue()

    def _scan_finish(self):
        self.btn_scan.configure(state="normal")
        self.btn_check.configure(state="normal")
        self.btn_start.configure(state="normal")
        self._refresh_firefox_cookies()
        self._update_auth_label()
        n = len(self.tasks)
        same = sum(1 for t in self.tasks if t["exists"] and t["same"])
        updated = sum(1 for t in self.tasks if t["exists"] and not t["same"])
        missing = sum(1 for t in self.tasks if not t["exists"])
        self.lbl.configure(
            text=(f"共 {n} 个" if n else "就绪"),
            text_color=pair("primary") if n else pair("muted"),
        )
        if n:
            self.log(f"完成：{n} 个文件（已有 {same}、有更新 {updated}、缺失 {missing}）。"
                     f"勾选后点「开始下载」。", "head")
        elif self._scan_login_failed:
            self.log("扫描中断：先把登录态弄好，再点「扫描」重试。", "err")
        else:
            self.log("扫描完成，但没找到任何附件。", "err")

        special_names = [f.get("name") or "" for f in self._special_pending]
        special_total = sum(int(f.get("length") or 0)
                            or core.parse_hsize(f.get("hsize") or "")
                            for f in self._special_pending)

        if self._scanning_all and self._all_reports:
            courses = len(self._all_reports)
            lines = []
            for rep in self._all_reports:
                s = rep["summary"]
                lines.append(f"{elide(rep['name'], 28)}：已有 {s['same']} / "
                             f"有更新 {s['updated']} / 缺失 {s['missing']}")
            preview = "\n".join(lines[:12]) + ("\n…" if len(lines) > 12 else "")
            summary = (f"共 {courses} 门课，{n} 个文件\n"
                       f"已有 {same} · 有更新 {updated} · 缺失 {missing} · "
                       f"特殊/大文件 {len(special_names)}\n\n{preview}")
            if special_names:
                preview_sp = "\n".join(special_names[:8]) + (
                    "\n…" if len(special_names) > 8 else "")
                ok = messagebox.askyesno(
                    "全部课程检查结果",
                    summary + f"\n\n检测到 {len(special_names)} 个特殊/大文件"
                    f"（约 {core.human(special_total)}）：\n{preview_sp}\n\n"
                    "是否把这些特殊/大文件也勾选下载？")
                if ok:
                    for t in self.tasks:
                        if t.get("special"):
                            t["var"].set(True)
                    self._update_sel()
            else:
                messagebox.showinfo("全部课程检查结果", summary)
            self.log(summary, "head")
        elif self._checking_local and self._special_pending:
            preview = "\n".join(special_names[:8]) + (
                "\n…" if len(special_names) > 8 else "")
            if messagebox.askyesno(
                    "特殊 / 大文件",
                    f"检测到 {len(special_names)} 个视频 / 安装包 / 压缩包 / 大文件"
                    f"（约 {core.human(special_total)}）：\n\n{preview}\n\n"
                    "是否一起下载？\n（选「否」会保留在列表里，默认不勾选）"):
                for t in self.tasks:
                    if t.get("special"):
                        t["var"].set(True)
                self._update_sel()
                self.log(f"特殊/大文件已勾选 {len(special_names)} 个", "ok")
            else:
                self.log("特殊/大文件保留在列表里、默认不勾；需要时手动勾选即可。",
                         "dim")

        self._checking_local = False
        self._scanning_all = False
        self._all_reports = []
        self._special_pending = []
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

        self._launch_jobs(job_list)

    def _launch_jobs(self, job_list):
        mode = MODE_LABELS.get(self.mode.get(), "orig")
        want_original = mode in ("orig", "both")
        want_pdf = mode in ("pdf", "both")
        pdf_fallback = bool(self.pdf_fallback.get())
        video_dl = bool(self.video_dl.get())
        video_original = video_dl and self.video_mode.get() == "原件"
        conc = max(1, int(self.jobs.get() or 4))
        auto_skip = bool(self.auto_skip.get())
        cookie, cookie_src = self._resolve_cookie()

        for _, _, d, _, _ in job_list:
            os.makedirs(d, exist_ok=True)

        d = load_settings()
        d.update({
            "outdir": self.outdir.get(),
            "mode": mode,
            "jobs": conc,
            "auto_skip": auto_skip,
            "pdf_fallback": pdf_fallback,
            "video": video_dl,
            "video_original": video_original,
            "cookie": self.cookie.get(),
            "appearance": self.appearance.get(),
            "accent": self.accent,
            "scan_cache": bool(self.scan_cache.get()),
            "levels": {
                "course": bool(self.lv_course.get()),
                "source": bool(self.lv_source.get()),
                "unit": bool(self.lv_unit.get()),
                "chapter": bool(self.lv_chapter.get()),
            },
        })
        save_settings(d)

        self._failed_jobs = []
        self.btn_retry.pack_forget()
        self._save_queue()

        self.stop_evt.clear()
        self.total = len(job_list)
        self.done = self.ok = self.fail = self.skip = 0
        self.t0 = time.time()
        self._progress(0)
        self.lbl.configure(text=f"0 / {self.total}", text_color=pair("muted"))
        self.btn_start.configure(state="disabled")
        self.btn_scan.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.pb.set(0)
        self.pb.pack(side="left", fill="x", expand=True, padx=(6, 12),
                     before=self.lbl)

        dirs = sorted({d for _, _, d, _, _ in job_list})
        self.log(f"\n共 {len(job_list)} 个文件 → {dirs[0] if len(dirs) == 1 else '多目录'}", "head")
        self.log(f"模式: {self.mode.get()}　并发: {conc}", "dim")
        self.log(
            "登录态: " + (cookie_src or "无")
            + ("" if want_original else "　(仅下载 PDF)")
            + ("" if (want_original or not pdf_fallback) else "，无 PDF 版的下原件"),
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
            args=(job_list, want_original, want_pdf, cookie, conc,
                  pdf_fallback, video_dl, video_original),
            daemon=True,
        )
        self.worker.start()

    def _retry_failed(self):
        if not self._failed_jobs:
            return
        if self.worker and self.worker.is_alive():
            return
        jobs = list(self._failed_jobs)
        self._failed_jobs = []
        self.btn_retry.pack_forget()
        self.log(f"\n重试 {len(jobs)} 个失败项…", "head")
        self._launch_jobs(jobs)

    def _run(self, job_list, want_original, want_pdf, cookie, conc,
             pdf_fallback=False, video_dl=False, video_original=False):
        with ThreadPoolExecutor(max_workers=conc) as ex:
            futs = {}
            for job in job_list:
                kind, payload, outdir, hint, overwrite = job
                if self.stop_evt.is_set():
                    break
                if kind == "data":
                    fut = ex.submit(core.download_data_one, payload, outdir, hint,
                                    cookie, self.timeout, overwrite)
                else:
                    fut = ex.submit(core.process_one, payload, outdir, want_original,
                                    want_pdf, cookie, self.timeout, overwrite, hint,
                                    pdf_fallback, video_dl, video_original)
                futs[fut] = job
            for fut in as_completed(futs):
                job = futs[fut]
                try:
                    r = fut.result()
                except Exception as e:  # noqa: BLE001
                    r = {"ok": False, "error": str(e), "files": [],
                         "objectid": job[1], "filename": job[3] or job[1]}
                r["_job"] = job
                self.q.put(("result", r))
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
                elif kind == "account":
                    self._account_pending = False
                    self._account_name = payload or ""
                    self._update_auth_label()
                elif kind == "accounts":
                    self._fill_accounts(payload)
                elif kind == "course_list":
                    self._on_course_list(payload)
                elif kind == "update_progress":
                    got, total = payload
                    dlg = getattr(self, "_upd_dlg", None)
                    if dlg and dlg.winfo_exists():
                        try:
                            if total:
                                self._upd_pb.set(min(1.0, got / total))
                                self._upd_lbl.configure(
                                    text=f"下载中… {got * 100 // total}%  "
                                         f"({core.human(got)} / "
                                         f"{core.human(total)})")
                            else:
                                self._upd_lbl.configure(
                                    text=f"下载中… {core.human(got)}")
                        except Exception:  # noqa: BLE001
                            pass
                elif kind == "update_ready":
                    self._apply_update(*payload)
                elif kind == "update_failed":
                    self._update_failed(payload)
                elif kind == "live_cookie":
                    if payload and not self.cookie.get().strip():
                        self._live_cookie = payload
                        self._update_auth_label()
                        self._fetch_account_name_async()
                    elif not payload:
                        self._live_cookie = ""
                        self._update_auth_label()
                elif kind == "update":
                    self._on_update(payload)
                elif kind == "scan_files":
                    self._on_scan_files(*payload)
                elif kind == "need_login":
                    self._scan_login_failed = True
                    self._account_name = ""          # 登录态已经失效，别再挂着旧名字
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
        if r.get("skip"):
            self.skip += 1
            self.log(f"⏭ {r.get('filename') or r['objectid']}　"
                     f"（跳过：{r.get('skip_reason', '不支持')}）", "dim")
        elif r["ok"]:
            self.ok += 1
            extra = f"　({r['pagenum']} 页)" if r.get("pagenum") else ""
            self.log(f"✔ {r.get('filename')}{extra}", "ok")
            for dest, size, note in r["files"]:
                self.log(f"    {note}: {os.path.basename(dest)}　{core.human(size)}", "dim")
        else:
            self.fail += 1
            job = r.get("_job")
            if job and job not in self._failed_jobs:
                self._failed_jobs.append(job)
            self.log(f"✘ {r.get('filename') or r['objectid']}", "err")
            self.log(f"    {r['error']}", "err")

    def _finish(self):
        self.btn_start.configure(state="normal")
        self.btn_scan.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.pb.pack_forget()
        self.lbl.configure(
            text=f"完成 {self.ok}/{self.total}",
            text_color=pair("primary") if self.fail else pair("text"),
        )
        parts = f"成功 {self.ok}"
        if self.skip:
            parts += f" / 跳过 {self.skip}"
        parts += f" / 失败 {self.fail}"
        self.log(
            f"\n完成: {parts}，耗时 {time.time() - self.t0:.1f}s", "head"
        )
        self.log(f"目录: {os.path.abspath(self.outdir.get())}", "dim")
        self._save_queue()
        if self._failed_jobs:
            self.btn_retry.configure(text=f"重试失败 ({len(self._failed_jobs)})")
            # before 必须是「当前仍被 pack 管理」的控件：进度条刚被 pack_forget 掉，
            # 拿它当参照 Tk 会抛 TclError（window ... isn't packed），按钮就永远出不来。
            self.btn_retry.pack(side="left", padx=(10, 0), before=self.lbl)
            self.log(f"有 {len(self._failed_jobs)} 个失败项，点「重试失败」再试一次", "err")
        else:
            self.btn_retry.pack_forget()

    # ---------------- 队列持久化 ----------------
    def _save_queue(self):
        """把当前任务列表落盘，关掉程序 / 崩溃后下次还能接着下。"""
        self._save_queue_after = None
        if not self.tasks:
            return
        try:
            tasks = []
            for t in self.tasks:
                row: dict = {}
                for k, v in t.items():
                    if k == "var" or k.startswith("_"):
                        continue          # _rows 是 Tk 控件，不能序列化
                    if v is None or isinstance(v, (str, int, float, bool)):
                        row[k] = v
                row["checked"] = bool(t["var"].get())
                tasks.append(row)
            data = {
                "ts": time.time(),
                "outdir": self.outdir.get(),
                "mode": self.mode.get(),
                "jobs": self.jobs.get(),
                "levels": {
                    k: bool(getattr(self, f"lv_{k}").get())
                    for k in ("course", "source", "unit", "chapter")
                },
                "tasks": tasks,
            }
            tmp = QUEUE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, QUEUE_FILE)
        except Exception:  # noqa: BLE001
            pass

    def _restore_queue(self):
        # 扫描/检查刚清空列表时也不能把旧队列塞回来，否则会和刚扫出的结果混在一起
        if (self.tasks or (self.worker and self.worker.is_alive())
                or (self.scan_worker and self.scan_worker.is_alive())):
            return
        try:
            with open(QUEUE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            raw = data.get("tasks") or []
            if not raw:
                return
            # 保存目录不恢复：落点始终按当前设置（用户可能特意换了目录）
            if data.get("mode"):
                self.mode.set(data["mode"])
            if data.get("jobs"):
                self.jobs.set(str(data["jobs"]))
            lv = data.get("levels") or {}
            for k in ("course", "source", "unit", "chapter"):
                if k in lv:
                    getattr(self, f"lv_{k}").set(bool(lv[k]))
            for t in raw:
                t.setdefault("bytes", 0)
                t.setdefault("exists", False)
                t.setdefault("same", None)
                t.setdefault("group", "其他")
                t.setdefault("label", t.get("name") or "")
                t.setdefault("size", "")
                t.setdefault("scan_key", "")
                t["var"] = ctk.BooleanVar(value=bool(t.pop("checked", True)))
                self.tasks.append(t)
            self.scan_key = ""          # 旧查重结果是按旧设置算的，不能当当前结果
            self._render_tasks()
            ts = float(data.get("ts") or 0)
            when = time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "上次"
            self.log(f"已恢复上次的下载队列：{len(raw)} 个文件（{when}）", "head")
            self.lbl.configure(text=f"已恢复 {len(raw)} 个", text_color=pair("primary"))
        except Exception:  # noqa: BLE001
            pass

    def _on_close(self):
        busy = (self.worker and self.worker.is_alive()) or (
            self.scan_worker and self.scan_worker.is_alive()
        )
        if busy:
            if not messagebox.askokcancel("正在忙", "还有任务在跑，确定要退出吗？"):
                return
            self.stop_evt.set()
        self._save_queue()
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
