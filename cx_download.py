#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""超星/学习通 预览文件批量下载器

原理
----
学习通的文件预览页
    https://mooc1.chaoxing.com/ananas/modules/pub/preview.html?objectid=<32位hex>
背后会请求一个统一的状态接口
    GET https://mooc1.chaoxing.com/ananas/status/<objectid>
返回 JSON（docx / pptx / pdf / xlsx 都一样）：
    {
      "download": "http://d0.cldisk.com/download/<oid>?at_=..&ak_=..&ad_=..",  # 原始文件（带时效签名）
      "pdf":      "https://s3.cldisk.com/.../<oid>.pdf",                        # 已转好的预览 PDF
      "filename": "xxx.docx",
      "length":   320275,
      "pagenum":  19,
      "status":   "success"
    }
本脚本把「预览链接 / objectid」解析出来，调这个接口拿签名直链，然后下载原文件
（默认）或 PDF。

除了单个预览页，也支持**直接给课程页链接**，会自动扫两个入口并全部下载，
按「课程名」建子目录：

1. **章节页**的任务点附件（`/mooc2-ans/mycourse/studentcourse` → 逐章 cards）；
2. **资料页**的文件（`/mooc2-ans/coursedata/stu-datalist`，递归展开文件夹）。

两个入口用同一个课程页链接就能触发（URL 里带 courseid / clazzid 即可，
`pageHeader` 是几都不影响）。扫描需要登录态，脚本会直接读本机 Firefox 的
cookies.sqlite 复用登录，不用手动复制 Cookie；读不到再用 `--cookie` 手填。

用法
----
    # 直接把链接当参数
    python cx_download.py "https://mooc1.chaoxing.com/ananas/modules/pub/preview.html?objectid=xxx" ...

    # 课程页：整门课的任务点附件一次拉完
    python cx_download.py "https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/stu?courseid=..&clazzid=..&cpi=.."

    # 从文本文件读（每行一个链接或 objectid，支持 # 注释和空行）
    python cx_download.py -f links.txt

    # 从剪贴板 / 标准输入读
    python cx_download.py --clip
    Get-Clipboard | python cx_download.py

    # 只检查本地（不下载）：按课程名找本地目录，递归比对是否已有
    python cx_download.py "<课程页链接>" --check-local --local-root "D:\\课程"
    # 检查后只补缺失的普通文件；视频/安装包/压缩包/大文件要显式 --include-special
    python cx_download.py "<课程页链接>" --check-local --download-missing
    # 把检查结果写成 JSON
    python cx_download.py "<课程页链接>" --check-local --report-json report.json

    # 常用选项
    -o, --out DIR      保存目录（默认 ./cx_download）
    --check-local      只检查本地，不下载
    --local-root DIR   本地课程根目录（默认同 --out）
    --download-missing 配合 --check-local：只下缺失的普通文件
    --include-special  配合 --check-local：特殊/大文件也一起下
    --large-mb N       大文件阈值（默认 100 MB）
    --pdf              除原文件外，额外下载 PDF
    --pdf-only         只下载 PDF
    --jobs N           并发数（默认 4）
    --cookie STR       手动指定 Cookie（默认不需要，公开预览接口即可）
    --overwrite        已存在且大小一致时默认跳过，加此项强制重下
    --timeout N        单次请求超时秒数（默认 60）

无需登录：实测 /ananas/status/ 接口对预览文件是公开的。若某些文件返回 403，
再用 --cookie 带上浏览器里 mooc1.chaoxing.com 的 Cookie。
"""

from __future__ import annotations

import argparse
import configparser
import glob
import hashlib
import html as _html
import json
import os
import queue
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

__version__ = "1.4.7"
GITHUB_REPO = "Yum0Rin/cx-downloader"
GITHUB_URL = f"https://github.com/{GITHUB_REPO}"
RELEASES_URL = f"{GITHUB_URL}/releases/latest"

OBJECTID_RE = re.compile(r"(?:objectid=)?([0-9a-fA-F]{32})")
ILLEGAL_RE = re.compile(r'[\\/:*?"<>|\r\n\t]')

# 课程页链接：只要 URL 里同时出现 courseid / clazzid 就算。
# 注意别把它当 objectid —— 这种 URL 的 enc 参数本身就是 32 位 hex。
COURSE_URL_RE = re.compile(
    r"https?://[^\s\"'<>\\]*?chaoxing\.com/[^\s\"'<>\\]*?(?:courseid|courseId)=\d+[^\s\"'<>\\]*",
    re.I,
)


def log(msg: str) -> None:
    print(msg, flush=True)


def extract_objectids(text: str) -> list[str]:
    """从任意文本里抓 objectid，保序去重。

    会先剔除课程页链接，否则链接里的 enc=<32位hex> 会被误当成 objectid。
    """
    text = COURSE_URL_RE.sub(" ", text or "")
    seen: dict[str, None] = {}
    for m in OBJECTID_RE.finditer(text):
        seen.setdefault(m.group(1).lower(), None)
    return list(seen)


def extract_course_urls(text: str) -> list[str]:
    """从任意文本里抓课程页链接，保序去重。"""
    out: list[str] = []
    seen: set[str] = set()
    for m in COURSE_URL_RE.finditer(text or ""):
        u = m.group(0).rstrip(".,;:、。)】]")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def parse_course_url(url: str) -> dict | None:
    """从课程页链接里取 courseid / clazzid / cpi。取不到返回 None。"""
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url or "").query)
    except Exception:  # noqa: BLE001
        return None
    low = {k.lower(): v[0] for k, v in q.items() if v}
    # 课程页写 clazzid；作业/考试页写 classId —— 同一个值，两种拼法都认
    courseid = low.get("courseid")
    clazzid = low.get("clazzid") or low.get("classid")
    if not (courseid and clazzid):
        return None
    return {
        "courseid": courseid,
        "clazzid": clazzid,
        "cpi": low.get("cpi") or low.get("uid") or "",
        "url": url,
    }


def sanitize(name: str, fallback: str) -> str:
    # 网页标题里常混进 &nbsp;（U+00A0），统一成普通空格，免得目录名看着怪
    name = (name or "").replace("\u00a0", " ")
    name = ILLEGAL_RE.sub("_", name.strip()).strip(" .")
    name = re.sub(r"_{2,}", "_", name)
    if not name:
        name = fallback
    # Windows 路径长度保护
    if len(name) > 150:
        name = name[:150]
    return name


def split_ext(filename: str) -> tuple[str, str]:
    """拆出 (主干, 扩展名)。扩展名保留前导点，非法字符直接丢弃。"""
    stem, ext = os.path.splitext(filename or "")
    ext = re.sub(r"[^A-Za-z0-9.]", "", ext)
    if not ext.startswith(".") or len(ext) > 10:
        ext = ""
    return stem, ext


def resolve_name(name_hint: str | None, server_name: str | None, oid: str) -> str:
    """定最终落盘文件名。

    优先用课程页里显示的名字（比云盘存储名更贴近章节），但**扩展名以服务端返回的为准**，
    免得显示名和真实格式对不上；两边都没扩展名时退回服务端名字。
    """
    server_name = server_name or f"{oid}.bin"
    if name_hint:
        hint_stem, hint_ext = split_ext(name_hint)
        _, real_ext = split_ext(server_name)
        stem, ext = (hint_stem or name_hint), (hint_ext or real_ext)
    else:
        stem, ext = split_ext(server_name)
    return (sanitize(stem, oid) or oid) + ext


def predict_name(name_hint: str, oid: str) -> str | None:
    """扫描阶段（还没请求服务端）预判落盘名，用于提前查本地是否已有同名文件。

    预判不出来（显示名没带扩展名，真实格式未知）就返回 None，让调用方跳过预检查。
    """
    _, hint_ext = split_ext(name_hint or "")
    if not hint_ext:
        return None
    return resolve_name(name_hint, None, oid)


def parse_hsize(text: str) -> int:
    """把 "10.65 MB" 这类展示用大小换成字节数（估算用，不保证精确）。"""
    m = re.match(r"\s*([\d.]+)\s*(B|KB|MB|GB|TB)?\s*$", (text or "").strip(), re.I)
    if not m:
        return 0
    try:
        n = float(m.group(1))
    except ValueError:
        return 0
    unit = (m.group(2) or "B").upper()
    return int(n * {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}[unit])


def http_get(url: str, headers: dict[str, str], timeout: int) -> bytes:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_status(oid: str, cookie: str | None, timeout: int, retries: int = 3) -> dict:
    url = f"https://mooc1.chaoxing.com/ananas/status/{oid}"
    headers = {
        "User-Agent": UA,
        "Referer": f"https://mooc1.chaoxing.com/ananas/modules/pub/preview.html?objectid={oid}",
        "Origin": "https://mooc1.chaoxing.com",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    }
    if cookie:
        headers["Cookie"] = cookie

    last: Exception | None = None
    for attempt in range(retries):
        try:
            raw = http_get(url, headers, timeout)
            return json.loads(raw.decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (401, 403):
                raise RuntimeError(
                    f"HTTP {e.code} —— 该文件可能需要登录，请用 --cookie 带上浏览器 Cookie"
                ) from e
        except Exception as e:  # noqa: BLE001
            last = e
        if attempt < retries - 1:
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"请求状态接口失败: {last}")


def download(url: str, dest: str, timeout: int, retries: int = 3,
             cookie: str | None = None) -> int:
    """流式下载到 dest.tmp 再改名，避免半截文件。返回字节数。

    断点续传：出错（尤其冷节点读超时）时**保留 .tmp**，下一轮带 `Range` 从断点接着下，
    不再从 0 重来 —— 否则碰上「只吐一点数据就长时间停滞」的云盘节点会永远下不完。
    服务端若忽略 Range（照旧回 200）则回退为整包重下。

    注意: d0.cldisk.com 的 download 直链会校验 Referer，缺了会 403。
    cookie 只对 chaoxing 自己的域名传（云盘直链不需要，也不该把课程 cookie 发过去）。
    """
    tmp = dest + ".tmp"
    base_headers = {"User-Agent": UA, "Referer": "https://mooc1.chaoxing.com/"}
    if cookie:
        base_headers["Cookie"] = cookie
    last: Exception | None = None
    for attempt in range(retries):
        done = os.path.getsize(tmp) if os.path.exists(tmp) else 0
        headers = dict(base_headers)
        if done:
            headers["Range"] = f"bytes={done}-"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                # 服务端不支持 Range（忽略后仍回 200）→ 只能从头写
                if done and resp.status == 200:
                    done = 0
                with open(tmp, "ab" if done else "wb") as f:
                    while True:
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
            os.replace(tmp, dest)
            return done
        except urllib.error.HTTPError as e:
            # 416：Range 起点已到末尾 → .tmp 其实就是完整文件
            if e.code == 416 and done and os.path.exists(tmp):
                os.replace(tmp, dest)
                return done
            last = e
        except Exception as e:  # noqa: BLE001
            last = e
        if attempt < retries - 1:
            time.sleep(1.5 * (attempt + 1))
    # 全部失败才清掉半截 .tmp（中途失败要留着给下次续传）
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
    err = RuntimeError(f"下载失败: {last}")
    # 把 HTTP 状态码带出去，调用方才能针对 404 给「转码不存在」这种具体提示
    err.http_code = getattr(last, "code", None)
    raise err


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}GB"


# 视频任务点的扩展名 + 特征字段：这类对象不走文档下载接口（状态里只有
# dtoken/mp3/截图/duration，没有 download/pdf），单独识别出来标「跳过」而非「失败」。
VIDEO_EXTS = {
    ".mp4", ".m4v", ".avi", ".flv", ".mkv", ".mov", ".wmv", ".rmvb", ".rm",
    ".webm", ".ts", ".m3u8", ".f4v", ".3gp", ".mpg", ".mpeg", ".vob",
}


def _looks_like_video(st: dict, ext: str) -> bool:
    if ext.lower() in VIDEO_EXTS:
        return True
    # 没有可直接下载的地址，却带视频专有字段 → 判为视频
    return (
        not (st.get("download") or st.get("pdf"))
        and any(k in st for k in ("dtoken", "duration"))
    )


def process_one(
    oid: str,
    outdir: str,
    want_original: bool,
    want_pdf: bool,
    cookie: str | None,
    timeout: int,
    overwrite: bool,
    name_hint: str | None = None,
    fallback_original: bool = False,
    want_video: bool = False,
    video_original: bool = False,
) -> dict:
    res = {"objectid": oid, "ok": False, "files": [], "error": None, "skip": False}
    try:
        st = fetch_status(oid, cookie, timeout)
        status = st.get("status")

        fname = resolve_name(name_hint, st.get("filename"), oid)
        stem, ext = split_ext(fname)
        res["filename"] = fname   # 失败时也要带出文件名，别只甩 objectid

        jobs: list[tuple[str, str]] = []
        if _looks_like_video(st, ext) and want_video:
            # 视频任务点不走文档那套：`http` 是转码好的 mp4（几十 MB），
            # `download` 是原件（.mov 能到 800MB+）。默认拿转码，够看且省空间。
            if video_original and st.get("download"):
                jobs.append((st["download"], os.path.join(outdir, fname)))
            elif st.get("http"):
                jobs.append((st["http"], os.path.join(outdir, stem + ".mp4")))
            elif st.get("download"):
                jobs.append((st["download"], os.path.join(outdir, fname)))
        else:
            if want_original and st.get("download"):
                jobs.append((st["download"], os.path.join(outdir, fname)))
            if want_pdf:
                pdf_name = (sanitize(stem, oid) or oid) + ".pdf"
                if st.get("pdf"):
                    jobs.append((st["pdf"], os.path.join(outdir, pdf_name)))
                elif ext.lower() == ".pdf" and st.get("download"):
                    # 原件本身就是 PDF：服务端不做转码，所以没有 pdf 字段，
                    # status 往往是 "failed"（那是「转码状态」，不是「能不能下」）。
                    # 直接把 download 存成 .pdf，别因为 status 就判死。
                    jobs.append((st["download"], os.path.join(outdir, pdf_name)))
        if not jobs and _looks_like_video(st, ext):
            # 视频任务点（mp4 等）不走文档这套接口，直接标为「跳过」，不算失败
            res["skip"] = True
            res["skip_reason"] = "视频（选项里可开「视频也下载」）"
            return res
        if not jobs and want_pdf and not want_original and st.get("download"):
            # 「仅 PDF」模式下，csv/py/ipynb/npy/xlsx 这些格式服务端根本不转 PDF
            # （status 里只有 download，没有 pdf），原件明明能下。
            if fallback_original:
                jobs.append((st["download"], os.path.join(outdir, fname)))
            else:
                res["skip"] = True
                res["skip_reason"] = "该格式没有 PDF 版（可在选项里开「无 PDF 版时下原件」）"
                return res
        if not jobs:
            if status and status != "success":
                raise RuntimeError(f"接口返回 status={status!r}，且没给出可下载地址")
            raise RuntimeError("接口未返回可下载地址（download/pdf 都为空）")

        for url, dest in jobs:
            expect = st.get("length") if dest.endswith(ext) and want_original else None
            if (
                not overwrite
                and expect
                and os.path.exists(dest)
                and os.path.getsize(dest) == expect
            ):
                res["files"].append((dest, expect, "已存在，跳过"))
                continue
            if not overwrite and os.path.exists(dest) and not expect:
                res["files"].append((dest, os.path.getsize(dest), "已存在，跳过"))
                continue
            try:
                size = download(url, dest, timeout)
            except RuntimeError as e:
                if getattr(e, "http_code", None) == 404 and dest.endswith(".pdf") and st.get("download"):
                    # 云盘转码节点上没有这个 PDF：要么还没生成，要么根本没转。
                    # 原件是能下的，别让人以为整份文件坏了。
                    raise RuntimeError(
                        "云盘上没有这份 PDF 转码（404）—— 换「原件+PDF」或「仅原件」下原件"
                    ) from e
                raise
            res["files"].append((dest, size, "下载完成"))

        res["ok"] = True
        res["pagenum"] = st.get("pagenum")
    except Exception as e:  # noqa: BLE001
        res["error"] = str(e)
    return res


# ============================================================ 课程页扫描
#
# 思路：学习通的「章节」页把每个任务点渲染成一个带 data="{objectid,name,type,size,...}"
# 的标签。所以只要拿到章节列表，逐章请求
#     https://mooc1.chaoxing.com/mooc-ans/knowledge/cards?...&knowledgeid=<章节id>
# 把里面的任务点抠出来，就等于把整门课的附件列全了。
#
# 这一步需要登录态，直接复用本机 Firefox 的 cookies.sqlite（明文存储），
# 免去手动复制 Cookie。拿到 objectid 之后走的是原来那套 /ananas/status/ 直链。

# 附件容器的 class 名改过好几版：老页面是 ans-attach-online，新页面把任务点包进
# ans-attach-ct 里的 downloadfile iframe，编辑器插进正文的文档又是 ans-insertdoc-retract。
# 按 class 名认必然漏（实测同一门课 42 章，只认 class 会漏掉 41/57 个），所以只认
# 「带 objectid 的 data JSON」这个共同点。
ATTACH_DATA_RE = re.compile(r'data="(\{[^"]*?objectid[^"]*?\})"')


def firefox_roots() -> list[str]:
    """Firefox 放 profiles 的候选根目录（按平台给，Windows / macOS / Linux）。

    Linux 上还要兼顾 snap 版（`~/snap/firefox/common/.mozilla/firefox`）——
    系统包、官方 deb、snap 三种装的路径各不相同。
    """
    home = os.path.expanduser("~")
    if sys.platform.startswith("win"):
        return [os.path.join(os.environ.get("APPDATA", ""), "Mozilla", "Firefox")]
    if sys.platform == "darwin":
        return [os.path.join(home, "Library", "Application Support", "Firefox")]
    return [
        os.path.join(home, ".mozilla", "firefox"),
        os.path.join(home, "snap", "firefox", "common", ".mozilla", "firefox"),
    ]


def firefox_root() -> str:
    """返回实际存在的 Firefox 根目录，都不在就给首选那个。"""
    roots = firefox_roots()
    for r in roots:
        if os.path.isdir(r):
            return r
    return roots[0]


def find_firefox_profiles() -> list[str]:
    """列出所有 Firefox 配置目录（先读 profiles.ini，再兜底扫目录）。"""
    profs: list[str] = []

    def _add(p: str) -> None:
        p = os.path.normpath(p)
        if p not in profs:
            profs.append(p)

    for root in firefox_roots():
        if not os.path.isdir(root):
            continue
        try:
            cp = configparser.ConfigParser()
            cp.read(os.path.join(root, "profiles.ini"), encoding="utf-8")
            for sec in cp.sections():
                if not sec.lower().startswith("profile"):
                    continue
                p = cp.get(sec, "Path", fallback="")
                if not p:
                    continue
                if cp.get(sec, "IsRelative", fallback="1") == "1":
                    p = os.path.join(root, p)
                _add(p)
        except Exception:  # noqa: BLE001
            pass
        # 兜底扫目录：Windows 在 <root>/Profiles/*，Linux / macOS 直接铺在 <root>/*
        # （只收真正的 profile 目录 —— 带上 prefs.js 才算，躲开 Crash Reports 之类）
        for pattern in ("Profiles/*", "*"):
            for p in glob.glob(os.path.join(root, pattern)):
                if os.path.isfile(os.path.join(p, "prefs.js")):
                    _add(p)
    return [p for p in profs if os.path.isdir(p)]


def load_firefox_cookies(profile: str | None = None) -> tuple[str | None, list[dict]]:
    """读 Firefox 里 *.chaoxing.com 的 cookie。

    Firefox 不加密 cookie 值，但库文件运行时被占用，所以先连 -wal/-shm 一起拷出来
    再读。返回 (配置目录, [{host,name,value,path}])，一个都读不到就是 (None, [])。
    """
    profs = [profile] if profile else find_firefox_profiles()
    best: tuple[str, list[dict]] | None = None
    for prof in profs:
        src = os.path.join(prof, "cookies.sqlite")
        if not os.path.exists(src):
            continue
        tmp = os.path.join(tempfile.gettempdir(), f"cx_ck_{os.getpid()}.sqlite")
        for suf in ("", "-wal", "-shm"):
            try:
                if os.path.exists(src + suf):
                    shutil.copy2(src + suf, tmp + suf)
            except Exception:  # noqa: BLE001
                pass
        rows: list[dict] = []
        try:
            con = sqlite3.connect(tmp)
            try:
                rows = [
                    {"host": h, "name": n, "value": v or "", "path": p or "/",
                     "ctx": ctx or ""}
                    for h, n, v, p, ctx in con.execute(
                        "select host, name, value, path, originAttributes from moz_cookies "
                        "where host like '%chaoxing%'"
                    ).fetchall()
                ]
            finally:
                con.close()
        except Exception:  # noqa: BLE001
            rows = []
        finally:
            for suf in ("", "-wal", "-shm"):
                try:
                    os.remove(tmp + suf)
                except OSError:
                    pass
        if rows and (best is None or len(rows) > len(best[1])):
            best = (prof, rows)
    if best is None:
        return None, []
    return best


def cookie_header_from_rows(rows: list[dict]) -> str:
    """把 cookie 行拼成 Cookie: 请求头。同名 cookie 只取第一个。"""
    seen: set[str] = set()
    parts: list[str] = []
    for r in rows:
        if r["name"] in seen:
            continue
        seen.add(r["name"])
        parts.append(f'{r["name"]}={r["value"].strip()}')
    return "; ".join(parts)


def firefox_cookie_header(profile: str | None = None) -> str:
    """拼成 Cookie: 请求头。同名 cookie 只取第一个。"""
    _, rows = load_firefox_cookies(profile)
    return cookie_header_from_rows(rows)


MARIONETTE_PORT = 2828


def _marionette_recv(sock) -> list:
    buf = b""
    while b":" not in buf:
        chunk = sock.recv(1)
        if not chunk:
            raise ConnectionError("Marionette 连接已关闭")
        buf += chunk
    length, rest = buf.split(b":", 1)
    size = int(length)
    data = rest
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("Marionette 连接已关闭")
        data += chunk
    return json.loads(data.decode("utf-8"))


def _marionette_call(sock, seq: int, name: str, params: dict | None = None):
    payload = json.dumps([0, seq, name, params or {}],
                         ensure_ascii=False).encode("utf-8")
    sock.sendall(str(len(payload)).encode() + b":" + payload)
    msg = _marionette_recv(sock)
    if len(msg) > 2 and msg[2]:
        raise RuntimeError(str(msg[2]))
    return msg[3]


def _cookie_from_marionette(c: dict) -> dict:
    return {
        "host": c.get("domain") or "",
        "name": c.get("name") or "",
        "value": c.get("value") or "",
        "path": c.get("path") or "/",
        "ctx": "live",
    }


def marionette_cookie_groups(port: int = MARIONETTE_PORT,
                             timeout: float = 8.0) -> list[list[dict]]:
    """从**正在运行**的 Firefox 读实时 Cookie（需要 Firefox 以 --marionette 启动）。

    学习通的登录票据（_uid / p_auth_token / vc3 等）经常只在 Firefox 内存里，
    磁盘 cookies.sqlite 里根本没有，所以「识别当前登录的号」必须读活浏览器。
    Marionette 是 Firefox 自带的调试协议，不需要额外依赖。

    每个浏览上下文（窗口 / 容器）各返回一组 cookie；不同容器可以登录不同账号。
    读不到（没开 Marionette / 端口被占 / 超时）就返回 []，调用方回退磁盘方案。
    """
    try:
        sock = socket.create_connection(("127.0.0.1", port),
                                        timeout=min(1.5, timeout))
        sock.settimeout(timeout)
        _marionette_recv(sock)                       # 握手
        _marionette_call(sock, 1, "WebDriver:NewSession", {"capabilities": {}})
    except Exception:  # noqa: BLE001
        return []

    groups: list[list[dict]] = []
    seq = 2
    tmp_window: str | None = None
    try:
        handles = _marionette_call(sock, seq, "WebDriver:GetWindowHandles") or []
        for h in handles:
            seq += 1
            _marionette_call(sock, seq, "WebDriver:SwitchToWindow", {"handle": h})
            seq += 1
            cookies = _marionette_call(sock, seq, "WebDriver:GetCookies") or []
            grp = [_cookie_from_marionette(c) for c in cookies
                   if "chaoxing" in (c.get("domain") or "")]
            if grp:
                groups.append(grp)
        if not groups:
            # 当前没有学习通标签：临时开一个读一次，读完关掉
            seq += 1
            win = _marionette_call(sock, seq, "WebDriver:NewWindow", {"type": "tab"})
            tmp_window = win.get("handle")
            seq += 1
            _marionette_call(sock, seq, "WebDriver:Navigate",
                             {"url": "https://i.mooc.chaoxing.com/"})
            time.sleep(1.2)
            seq += 1
            cookies = _marionette_call(sock, seq, "WebDriver:GetCookies") or []
            grp = [_cookie_from_marionette(c) for c in cookies
                   if "chaoxing" in (c.get("domain") or "")]
            if grp:
                groups.append(grp)
    except Exception:  # noqa: BLE001
        pass
    finally:
        if tmp_window:
            try:
                seq += 1
                _marionette_call(sock, seq, "WebDriver:CloseWindow", {})
            except Exception:  # noqa: BLE001
                pass
        try:
            seq += 1
            _marionette_call(sock, seq, "WebDriver:DeleteSession", {})
        except Exception:  # noqa: BLE001
            pass
        try:
            sock.close()
        except Exception:  # noqa: BLE001
            pass

    # 同一容器的多个标签会读到同一份 cookie，按内容去重
    uniq: list[list[dict]] = []
    seen: set[str] = set()
    for grp in groups:
        key = ",".join(sorted(f'{r["name"]}={r["value"]}' for r in grp))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(grp)
    return uniq


def firefox_accounts(timeout: int = 12, probe_names: bool = True,
                     live: bool = True) -> list[dict]:
    """列出 Firefox 里能读到的所有学习通账号（优先读运行中的浏览器）。

    顺序：先用 Marionette 读活 Cookie（当前登录的号，含只在内存里的票据），
    再扫磁盘上各 profile / 容器的 Cookie 作为补充（历史登录）。
    同一个 _uid 只留一份，实时读到的优先。

    返回 [{name, uid, profile_name, ctx, cookie, count, live}]；
    读不到完整登录态（没有 _uid）的也列出来，name 为空。
    """
    groups: list[tuple[str, bool, str, list[dict]]] = []   # 来源名 / 是否实时 / profile / 行
    if live:
        for i, grp in enumerate(marionette_cookie_groups()):
            label = "Firefox 实时" + (f" #{i + 1}" if i else "")
            groups.append((label, True, "", grp))
    for prof in find_firefox_profiles():
        _, rows = load_firefox_cookies(prof)
        if not rows:
            continue
        by_ctx: dict[str, list[dict]] = {}
        for r in rows:
            by_ctx.setdefault(r.get("ctx") or "", []).append(r)
        base = os.path.basename(prof.rstrip("\\/"))
        for ctx, grp in by_ctx.items():
            groups.append((base, False, prof, grp))

    out: list[dict] = []
    for label, is_live, prof, grp in groups:
        header = cookie_header_from_rows(grp)
        uid = next((r["value"].strip() for r in grp
                    if r["name"] in ("_uid", "UID") and r["value"].strip()), "")
        if not uid:
            # 没有登录票据的残缺 Cookie 选也没用，不列出来
            continue
        name = ""
        if probe_names:
            try:
                name = fetch_account_name(header, timeout)
            except Exception:  # noqa: BLE001
                name = ""
        out.append({
            "name": name,
            "uid": uid,
            "profile": prof,
            "profile_name": label,
            "ctx": grp[0].get("ctx") or "",
            "cookie": header,
            "count": len(grp),
            "live": is_live,
        })
    # 同一个号可能在多个 profile / 容器 / 实时+磁盘里：实时优先，其次 cookie 最全的
    dedup: dict[str, dict] = {}
    for a in out:
        key = a["uid"] or f'{a["profile_name"]}|{a["ctx"]}'
        old = dedup.get(key)
        if old is None or (a["live"] and not old["live"]) or (
                a["live"] == old["live"] and a["count"] > old["count"]):
            dedup[key] = a
    return sorted(dedup.values(),
                  key=lambda a: (not a["live"], not a["name"], a["name"] or a["uid"]))



class LoginRequired(RuntimeError):
    """服务器返回的是登录页 —— 链接本身不含登录态，缺 cookie 就会这样。"""


# 判定「拿到的是登录页」的几个特征。超星未登录时统一跳到 passport 登录页。
LOGIN_MARKERS = (
    "<title>用户登录</title>",
    "passport2.chaoxing.com",
    'id="loginBox"',
    'name="uname"',
)


def looks_like_login(html: str) -> bool:
    head = (html or "")[:6000]
    return any(m in head for m in LOGIN_MARKERS)


def _get_text(url: str, cookie: str | None, timeout: int,
              referer: str | None = None, retries: int = 3) -> str:
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
    if cookie:
        headers["Cookie"] = cookie
    last: Exception | None = None
    for attempt in range(retries):
        try:
            page = http_get(url, headers, timeout).decode("utf-8", "replace")
            if looks_like_login(page):
                raise LoginRequired("登录态失效：服务器返回的是登录页")
            return page
        except LoginRequired:
            raise
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (401, 403):
                raise LoginRequired(f"HTTP {e.code} —— 服务器拒绝了这次请求") from e
        except Exception as e:  # noqa: BLE001
            last = e
        if attempt < retries - 1:
            time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"请求失败: {last}")


def _clean(text: str) -> str:
    """网页里取出来的文本统一收拾一下：去 &nbsp;、压空白、去首尾。"""
    return re.sub(r"\s+", " ", _html.unescape(text or "").replace("\u00a0", " ")).strip()


ACCOUNT_PAGE = "https://passport2.chaoxing.com/mooc/accountManage"


def fetch_account_name(cookie: str | None, timeout: int = 15) -> str:
    """当前登录账号的姓名，用来确认「现在用的是哪个号」。

    手动 Cookie 和 Firefox 登录态可能属于不同账号，界面上不显示姓名就分不出来。
    取不到（没登录 / 断网 / 接口改版）一律返回空串 —— 这行只是提示，不能挡扫描。
    """
    if not cookie:
        return ""
    try:
        page = _get_text(ACCOUNT_PAGE, cookie, timeout,
                         referer="https://i.chaoxing.com/", retries=1)
    except Exception:  # noqa: BLE001
        return ""
    m = (re.search(r'id="messageName"[^>]*\bvalue="([^"]*)"', page)
         or re.search(r'id="messageName"[^>]*>([^<]*)<', page))
    return _clean(m.group(1)) if m else ""


def fetch_course_name(cookie: str | None, url: str, timeout: int) -> str:
    """课程名：课程主页 <title> 就是「学期 + 课程名」。取不到就返回空串。"""
    try:
        page = _get_text(url, cookie, timeout, retries=1)
    except LoginRequired:
        raise           # 让调用方知道是「没登录」，而不是「取不到名字」
    except Exception:  # noqa: BLE001
        return ""
    m = re.search(r"<title>(.*?)</title>", page, re.S)
    if not m:
        return ""
    name = _clean(m.group(1))
    if name in ("", "学生学习页面", "课程"):
        return ""
    return name


def fetch_course_name_robust(cookie: str | None, url: str,
                             timeout: int = 60) -> str:
    """取课程名。章节页 <title> 常为空，回退到「资料」页的隐藏 courseName。

    课程主页的 t 参数是会话里的值，自己拼时间戳只会拿到空壳；资料页不需要 t，
    而且页内直接有 <input id="courseName" value="...">。
    """
    name = fetch_course_name(cookie, url, timeout)
    if name:
        return name
    info = parse_course_url(url)
    if not info:
        return ""
    try:
        q = urllib.parse.urlencode({
            "courseid": info["courseid"], "clazzid": info["clazzid"],
            "cpi": info["cpi"], "ut": "s",
        })
        page = _get_text(
            "https://mooc2-ans.chaoxing.com/mooc2-ans/coursedata/stu-datalist?" + q,
            cookie, timeout, referer="https://mooc2-ans.chaoxing.com/")
        m = re.search(r'id="courseName"[^>]*value="([^"]*)"', page)
        if m:
            return _clean(_html.unescape(m.group(1)))
    except Exception:  # noqa: BLE001
        pass
    return ""


def fetch_course_list(cookie: str | None, timeout: int = 60,
                      include_history: bool = False) -> list[dict]:
    """取当前账号的全部课程（courseid / clazzid / cpi / name）。

    `backclazzdata` 是学习通自己列表页用的接口，一次返回全部课程；
    cfid == -1 是「没放进文件夹」的当前课程，其它 cfid 是历史课程文件夹。
    """
    url = "https://mooc1-api.chaoxing.com/mycourse/backclazzdata?view=json&rss=1"
    page = _get_text(url, cookie, timeout, referer="https://i.mooc.chaoxing.com/")
    try:
        data = json.loads(page)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"课程列表不是 JSON：{e}") from e

    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for ch in data.get("channelList") or []:
        if not include_history and ch.get("cfid") != -1:
            continue
        c = ch.get("content") or {}
        for cd in ((c.get("course") or {}).get("data") or []):
            cid, clz = cd.get("id"), c.get("id")
            if not (cid and clz):
                continue
            key = (str(cid), str(clz))
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "courseid": str(cid),
                "clazzid": str(clz),
                "cpi": str(c.get("cpi") or ""),
                "name": cd.get("name") or "",
                "beginDate": c.get("beginDate") or "",
                "endDate": c.get("endDate") or "",
                "isstart": bool(c.get("isstart")),
                "isretire": bool(c.get("isretire")),
            })
    return out


def course_time_status(begin_date: str, end_date: str, now=None) -> str:
    """按开课/结课时间判断课程状态。日期格式是「2026-08-26 08:22」。"""
    from datetime import datetime

    now = now or datetime.now()
    fmt = "%Y-%m-%d %H:%M"
    try:
        b = datetime.strptime((begin_date or "").strip(), fmt)
    except Exception:  # noqa: BLE001
        b = None
    try:
        e = datetime.strptime((end_date or "").strip(), fmt)
    except Exception:  # noqa: BLE001
        e = None
    if b and now < b:
        return "未开始"
    if e and now > e:
        return "已结束"
    if b or e:
        return "进行中"
    return "未知"


def fetch_course_chapters(cookie: str | None, courseid: str, clazzid: str,
                          cpi: str, timeout: int = 60) -> list[dict]:
    """取章节列表。返回 [{kid, title, unit}]，unit 是「基础信息 / 教学课件」这类分组名。"""
    q = urllib.parse.urlencode(
        {"courseid": courseid, "clazzid": clazzid, "cpi": cpi, "ut": "s"}
    )
    url = "https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/studentcourse?" + q
    page = _get_text(url, cookie, timeout,
                     referer="https://mooc2-ans.chaoxing.com/")

    chapters: list[dict] = []
    blocks = page.split('<div class="chapter_unit">')[1:]
    for block in blocks:
        mu = re.search(r'<span title="([^"]*)"', block)
        unit = _clean(mu.group(1)) if mu else ""
        for m in re.finditer(r'<div class="chapter_item"\s+id="cur(\d+)"([^>]*)>', block):
            mt = re.search(r'title="([^"]*)"', m.group(2))
            chapters.append({
                "kid": m.group(1),
                "title": _clean(mt.group(1)) if mt else m.group(1),
                "unit": unit,
            })
    if not chapters:
        raise RuntimeError(
            "没解析出章节列表 —— 该链接可能不是章节页（应含 courseid / clazzid）"
        )
    return chapters


def parse_attachments(page: str) -> list[dict]:
    """从章节页 HTML 里抠附件，按 objectid 去重。

    单独拆出来是为了能脱网自检（`selftest.py` 拿一段三种写法混排的样例喂进来）。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for md in ATTACH_DATA_RE.finditer(page):
        try:
            info = json.loads(_html.unescape(md.group(1)))
        except Exception:  # noqa: BLE001
            continue
        oid = str(info.get("objectid") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{32}", oid) or oid in seen:
            continue
        seen.add(oid)
        out.append({
            "objectid": oid,
            "name": _clean(info.get("name") or ""),
            "type": info.get("type") or "",
            "hsize": info.get("hsize") or "",
            "jobid": str(info.get("jobid") or info.get("_jobid") or ""),
        })
    return out


SCAN_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cx_download_cache")
SCAN_CACHE_TTL = 10 * 60          # 10 分钟内重复扫同一门课直接复用


def _scan_cache_file(key: str) -> str:
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return os.path.join(SCAN_CACHE_DIR, f"scan-{digest}.json")


def load_scan_cache(key: str, ttl: int = SCAN_CACHE_TTL) -> dict | None:
    """读扫描缓存；过期 / 损坏都当作没有。"""
    try:
        with open(_scan_cache_file(key), encoding="utf-8") as f:
            data = json.load(f)
        if time.time() - float(data.get("ts") or 0) > ttl:
            return None
        result = data.get("result")
        return result if isinstance(result, dict) else None
    except Exception:  # noqa: BLE001
        return None


def save_scan_cache(key: str, result: dict) -> None:
    try:
        os.makedirs(SCAN_CACHE_DIR, exist_ok=True)
        with open(_scan_cache_file(key), "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "result": result}, f,
                      ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass


def fetch_chapter_attachments(cookie: str | None, courseid: str, clazzid: str,
                              cpi: str, kid: str, timeout: int = 60,
                              max_cards: int = 20) -> list[dict]:
    """取某一章节下的所有附件（任务点里的文档/PPT/PDF 等）。

    学习通把一章拆成多张「卡片」（PPT / 习题集 / 案例 / 课程思政…），每张卡
    用独立的 num 索引（0 起）。以前只取 num=0，后面的卡整张漏掉 —— 实测某门课
    的习题集扫不出来就是它。num 越界时服务端返回 <title> 为空的占位页，据此
    收尾；连吃两张空卡才停，避免某张没命名的卡把后面的卡截断。
    """
    out: list[dict] = []
    seen: set[str] = set()
    empty_streak = 0
    for num in range(max_cards):
        q = urllib.parse.urlencode({
            "clazzid": clazzid, "courseid": courseid, "knowledgeid": kid,
            "num": str(num), "ut": "s", "cpi": cpi, "mooc2": "1",
            "isMicroCourse": "false", "editorPreview": "0",
        })
        url = "https://mooc1.chaoxing.com/mooc-ans/knowledge/cards?" + q
        page = _get_text(url, cookie, timeout, referer="https://mooc1.chaoxing.com/")
        mt = re.search(r"<title>(.*?)</title>", page, re.S)
        card = _clean(mt.group(1)) if mt else ""
        atts = parse_attachments(page)
        for a in atts:
            if a["objectid"] in seen:
                continue
            seen.add(a["objectid"])
            a["card"] = card
            out.append(a)
        if not card and not atts:
            empty_streak += 1
            if empty_streak >= 2:
                break
        else:
            empty_streak = 0
    return out


def scan_course(cookie: str | None, url: str, timeout: int = 60,
                progress=None, use_cache: bool = True,
                workers: int = 4, name_hint: str | None = None) -> dict:
    """扫一门课，返回 {name, courseid, files:[{objectid,name,chapter,unit,...}]}。

    use_cache=True 时，10 分钟内扫过的同一门课直接读本地缓存（学习通重新登录 /
    老师刚传文件时用 GUI 里的「重新扫描」或关掉缓存）。章节的卡片请求会并发。
    """
    info = parse_course_url(url)
    if not info:
        raise RuntimeError("不是可识别的课程页链接（URL 里需要 courseid 与 clazzid）")

    def say(msg: str) -> None:
        if progress:
            progress(msg)

    cache_key = "|".join([info["courseid"], info["clazzid"], info.get("cpi") or "",
                          info.get("enc") or ""])
    if use_cache:
        cached = load_scan_cache(cache_key)
        if cached:
            n = len(cached.get("files") or [])
            say(f"用扫描缓存：{n} 个附件（10 分钟内扫过；要拿最新列表请再点一次「扫描」"
                f"或到设置里关掉缓存）")
            return cached

    name = (name_hint or fetch_course_name(cookie, url, timeout)
            or f"超星课程-{info['courseid']}")
    say(f"课程：{name}")
    chapters = fetch_course_chapters(
        cookie, info["courseid"], info["clazzid"], info["cpi"], timeout
    )
    say(f"章节：{len(chapters)} 个，开始逐章找附件…")

    files: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = [
            ex.submit(fetch_chapter_attachments, cookie, info["courseid"],
                      info["clazzid"], info["cpi"], ch["kid"], timeout)
            for ch in chapters
        ]
        for i, (ch, fut) in enumerate(zip(chapters, futs), 1):
            try:
                atts = fut.result()
            except Exception as e:  # noqa: BLE001
                say(f"  [{i}/{len(chapters)}] {ch['title']} 读取失败：{e}")
                continue
            for a in atts:
                a["source"] = "章节"
                a["chapter"] = ch["title"]
                a["unit"] = ch["unit"]
                files.append(a)
            say(f"  [{i}/{len(chapters)}] {ch['title']} → {len(atts)} 个附件")

    seen: set[str] = set()
    uniq: list[dict] = []
    for f in files:
        if f["objectid"] in seen:
            continue
        seen.add(f["objectid"])
        uniq.append(f)
    result = {"name": name, "courseid": info["courseid"], "files": uniq}
    if use_cache:
        save_scan_cache(cache_key, result)
    return result


# ------------------------------------------------------------ 重复文件判定
#
# /ananas/status/ 返回的 crc 是 32 位 hex，但实测**不是文件内容的哈希**
# （md5/sha1 都对不上，应该是云盘内部对象号），所以不能拿它比对。
# 好在 d0.cldisk.com 支持 HTTP Range，于是「同一文件」= 字节数相同
# 且「前 64KB 的 md5」相同。代价只有 64KB 流量，不用整包重下。

HEAD_BYTES = 64 * 1024


def local_head_md5(path: str, nbytes: int = HEAD_BYTES) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        h.update(f.read(nbytes))
    return h.hexdigest()


def remote_probe(url: str, cookie: str | None = None, timeout: int = 60,
                 nbytes: int = HEAD_BYTES) -> tuple[int | None, str]:
    """一次 Range 请求同时拿到「远端总字节数」和「前 nbytes 字节的 md5」。

    服务端不一定支持 Range；不支持时会返回整包，这里只 read(nbytes)，
    总长度就退回用 Content-Length，结果照样正确。
    """
    headers = {
        "User-Agent": UA,
        "Referer": "https://mooc1.chaoxing.com/",
        "Range": f"bytes=0-{nbytes - 1}",
    }
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        total: int | None = None
        cr = resp.headers.get("Content-Range") or ""
        if "/" in cr:
            try:
                total = int(cr.rsplit("/", 1)[-1])
            except ValueError:
                total = None
        if total is None:
            cl = resp.headers.get("Content-Length")
            if cl and cl.isdigit():
                total = int(cl)
        head = resp.read(nbytes)
    return total, hashlib.md5(head).hexdigest()


def compare_url_with_local(cookie: str | None, url: str, path: str,
                           timeout: int = 60) -> tuple[bool, int | None]:
    """本地 path 与远端 url 是不是同一份文件。返回 (是否相同, 远端字节数)。"""
    total, head_md5 = remote_probe(url, cookie, timeout)
    if not total or os.path.getsize(path) != total:
        return False, total
    return head_md5 == local_head_md5(path), total


def compare_with_remote(cookie: str | None, oid: str, path: str,
                        timeout: int = 60) -> tuple[bool, int | None]:
    """本地 path 与远端 oid 是不是同一份文件。返回 (是否相同, 远端字节数)。"""
    st = fetch_status(oid, cookie, timeout)
    length = st.get("length")
    url = st.get("download")
    if not url:
        return False, length
    try:
        same, total = compare_url_with_local(cookie, url, path, timeout)
    except Exception:  # noqa: BLE001
        return False, length
    return same, (total or length)


def compare_pdf_with_remote(cookie: str | None, oid: str, path: str,
                            timeout: int = 60) -> tuple[bool, int | None]:
    """同上，但比的是「转码 PDF」。本地那份是 .pdf 时不能拿原件去比 —— 字节数根本不同。"""
    st = fetch_status(oid, cookie, timeout)
    url = st.get("pdf")
    if not url:
        return False, st.get("length")
    try:
        same, total = compare_url_with_local(cookie, url, path, timeout)
    except Exception:  # noqa: BLE001
        return False, st.get("length")
    return same, total


def resolve_course_files(cookie: str | None, files: list[dict], outdir: str,
                         timeout: int = 60, progress=None,
                         workers: int = 6, want_pdf_only: bool = False,
                         dir_of=None) -> list[dict]:
    """给扫描结果补上「落到哪个路径 / 本地是否已有 / 是否同一份」。

    只有本地**已经存在同名文件**时才会去请求服务端比对，全新文件零额外请求。

    want_pdf_only 要跟下载模式对上：「仅 PDF」时落盘名是 `<stem>.pdf`，
    查重还按原名去找就永远是「新文件」（踩过）。所以两种候选都看一遍。
    dir_of(f) 用来算每项真正的落点目录（按目录层级拼出来的分组/章节目录），
    不传就一律用 outdir —— 传错会「明明下过却全标新文件」。
    """
    pending: list[dict] = []
    for f in files:
        folder = dir_of(f) if dir_of else outdir
        f["outdir"] = folder
        key = f.get("objectid") or f.get("dataid") or "file"
        name = f.get("name") or ""
        stem, _ = split_ext(name)
        orig_name = predict_name(name, key)          # 原件名（名字没扩展名时是 None）
        pdf_name = (sanitize(stem, key) or key) + ".pdf" if name else None
        order = [pdf_name, orig_name] if want_pdf_only else [orig_name, pdf_name]
        hit = next((t for t in order if t and os.path.exists(os.path.join(folder, t))), None)
        f["target"] = hit or next((t for t in order if t), None)
        f["path"] = os.path.join(folder, f["target"]) if f["target"] else ""
        f["exists"] = hit is not None
        # 命中的是不是 PDF 转码，决定拿哪个远端地址比内容
        f["_is_pdf"] = bool(hit and hit == pdf_name and hit != orig_name)
        f["same"] = None
        f["length"] = None
        if f["exists"]:
            pending.append(f)

    if not pending:
        return files

    def check(f: dict) -> tuple[bool, int | None]:
        # 「资料」页的文件没有 objectid，只有直链，走 URL 版比对
        if f.get("url"):
            return compare_url_with_local(cookie, f["url"], f["path"], timeout)
        if f.get("_is_pdf"):
            return compare_pdf_with_remote(cookie, f["objectid"], f["path"], timeout)
        return compare_with_remote(cookie, f["objectid"], f["path"], timeout)

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(check, f): f for f in pending}
        for fut in as_completed(futs):
            f = futs[fut]
            try:
                f["same"], f["length"] = fut.result()
            except Exception:  # noqa: BLE001
                f["same"] = False
            done += 1
            if progress:
                progress(f"校验已存在文件 {done}/{len(pending)}：{f['target']}")
    return files


# ------------------------------------------------------------ 本地课程目录查重
#
# 以前的查重只看「保存目录 + 层级勾选」算出来的那一个落点。用户手动把课件整理进
# 知识点/课件讲义、作业、课程文档 之后，层级就对不上，工具会把明明存在的文件标成
# 新文件。这里改成：先按课程名找到本地已有的课程文件夹，再递归扫整个文件夹。

INSTALLER_EXTS = {
    ".exe", ".msi", ".dmg", ".pkg", ".iso", ".apk", ".ipa",
    ".bin", ".img", ".deb", ".rpm",
}
ARCHIVE_EXTS = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz",
    ".cab", ".tgz",
}
LARGE_BYTES = 100 * 1024 * 1024


def _norm_text(s: str) -> str:
    s = (s or "").replace("\u00a0", " ")
    s = re.sub(r"[\s_\-（）()【】\[\].,，。·、]+", "", s.lower())
    return s


def _norm_stem(filename: str) -> str:
    """去掉扩展名和尾部 (1)(2) 这类重复标记，只留主名。"""
    stem, _ = split_ext(filename or "")
    stem = re.sub(r"\s*\(\d+\)\s*$", "", stem)
    return _norm_text(stem)


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度。课程名常有简称/别名，整串包含匹配不到，用它兜底。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for ca in a:
        cur = [0] * (len(b) + 1)
        for j, cb in enumerate(b, 1):
            if ca == cb:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def find_local_course_dir(base: str, course_name: str, courseid: str = "") -> str:
    """在保存根目录下找已有的课程文件夹。

    容忍「261学期 / 2026学年」前缀、括号别名、简称。按最长公共子串打分，
    分数太低（如两门课只共用一个「课程」）就不硬配。
    """
    if not base or not os.path.isdir(base):
        return ""
    target = re.sub(r"^(?:\d{3,4}学期|\d{4}学年|\d{4})", "", (course_name or "").strip())
    tn = _norm_text(target)
    if not tn:
        return ""
    best, best_score = "", 0.0
    for d in os.listdir(base):
        p = os.path.join(base, d)
        if not os.path.isdir(p):
            continue
        dn = _norm_text(d)
        if not dn:
            continue
        if tn in dn or dn in tn:
            return p
        common = _lcs_len(tn, dn)
        score = common / max(1, min(len(tn), len(dn)))
        # 括号里的别名（如「多元统计模型与方法(多元数据分析)」）单独再试一次
        for alias in re.findall(r"[（(]([^）)]+)[）)]", course_name or ""):
            an = _norm_text(alias)
            if an and (an in dn or dn in an):
                score = max(score, 1.0)
        if score > best_score:
            best, best_score = p, score
    return best if best_score >= 0.30 else ""


def collect_local_files(folder: str) -> list[dict]:
    """递归收集目录下所有文件，层级不限。"""
    out: list[dict] = []
    if not folder or not os.path.isdir(folder):
        return out
    for base, _dirs, files in os.walk(folder):
        for name in files:
            path = os.path.join(base, name)
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            out.append({
                "name": name,
                "path": path,
                "size": size,
                "ext": os.path.splitext(name)[1].lower(),
            })
    return out


def build_local_index(local_files: list[dict]) -> dict:
    by_name: dict[str, list[dict]] = {}
    by_size: dict[int, list[dict]] = {}
    for f in local_files:
        by_name.setdefault(_norm_stem(f["name"]), []).append(f)
        if f["size"]:
            by_size.setdefault(f["size"], []).append(f)
    return {"by_name": by_name, "by_size": by_size}


def match_local_file(name: str, length: int, index: dict) -> dict:
    """判断平台文件在本地是否已有。返回 {exists, same, local_path, match}。

    先按主名（去 (1) 后缀、不看扩展名）匹配；主名不中再用精确字节数兜底。
    同一章 PDF 版与 PPT 源文件主名相同，按「已有」处理，避免重复下载。
    """
    key = _norm_stem(name)
    hit = (index["by_name"].get(key) or [None])[0]
    if hit:
        remote_ext = os.path.splitext(name or "")[1].lower()
        local_ext = hit.get("ext") or ""
        office = {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"}
        if local_ext != remote_ext and local_ext in office and remote_ext in office:
            # 本地是平台 Office 原件转出来的 PDF，内容同一份，别算成「有更新」
            same = True
        else:
            # 主名相同但没取到平台字节数时，按「相同」处理：查重的目的是别重复下，
            # 宁可放过也不要每次因为拿不到 length 就重下一遍。
            same = (hit["size"] == length) if length else True
        return {"exists": True, "same": same, "local_path": hit["path"],
                "match": "name"}
    if length and length in index["by_size"]:
        hit = index["by_size"][length][0]
        return {"exists": True, "same": True, "local_path": hit["path"],
                "match": "size"}
    return {"exists": False, "same": False, "local_path": "", "match": ""}


def is_special_file(name: str, length: int = 0,
                    large_bytes: int = LARGE_BYTES) -> tuple[bool, str]:
    """视频 / 安装包 / 压缩包 / 大文件。专门提示，别混在普通课件里默认下载。"""
    ext = os.path.splitext(name or "")[1].lower()
    if ext in VIDEO_EXTS:
        return True, "视频"
    if ext in INSTALLER_EXTS:
        return True, "安装包"
    if ext in ARCHIVE_EXTS:
        return True, "压缩包"
    if length and large_bytes and length >= large_bytes:
        return True, f"大文件（{human(length)}）"
    return False, ""


def _file_length(f: dict, cookie: str | None, timeout: int) -> int:
    if f.get("length"):
        return int(f.get("length") or 0)
    try:
        if f.get("objectid"):
            return int(fetch_status(f["objectid"], cookie, timeout).get("length") or 0)
        if f.get("url"):
            total, _ = remote_probe(f["url"], cookie, timeout)
            return int(total or 0)
    except Exception:  # noqa: BLE001
        return 0
    return 0


def check_local_course(cookie: str | None, url: str, base: str,
                       timeout: int = 60, progress=None,
                       large_bytes: int = LARGE_BYTES,
                       use_cache: bool = True, workers: int = 4) -> dict:
    """扫描一门课，并对照本地课程文件夹（递归）判断已有/缺失/特殊。

    只读，不下载。返回 {name, local_dir, files, summary}。
    """
    info = parse_course_url(url)
    if not info:
        raise RuntimeError("不是可识别的课程页链接（URL 里需要 courseid 与 clazzid）")

    def say(msg: str) -> None:
        if progress:
            progress(msg)

    name = fetch_course_name_robust(cookie, url, timeout) or f"课程{info['courseid']}"
    local_dir = find_local_course_dir(base, name, info["courseid"])
    if local_dir:
        say(f"本地课程目录：{local_dir}")
    else:
        say("本地没找到对应课程目录，按缺失处理")

    files: list[dict] = []
    try:
        sc = scan_course(cookie, url, timeout, progress=progress,
                         use_cache=use_cache, workers=workers, name_hint=name)
        for f in sc["files"]:
            f["source"] = "章节"
            files.append(f)
    except Exception as e:  # noqa: BLE001
        say(f"章节扫描失败：{e}")
    try:
        for f in scan_course_data(cookie, info["courseid"], info["clazzid"],
                                  info["cpi"], timeout, progress=progress):
            f["source"] = "资料"
            files.append(f)
    except Exception as e:  # noqa: BLE001
        say(f"资料扫描失败：{e}")

    local_files = collect_local_files(local_dir)
    index = build_local_index(local_files)
    say(f"本地 {local_dir or '（无）'} 下共 {len(local_files)} 个文件")

    # 全部取精确字节数：只知道「本地有没有」不够，还要靠字节数判断本地版本是不是旧的
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(_file_length, f, cookie, timeout): f for f in files}
        for fut in as_completed(futs):
            f = futs[fut]
            try:
                f["length"] = fut.result()
            except Exception:  # noqa: BLE001
                f["length"] = 0
    for f in files:
        m = match_local_file(f.get("name") or "", int(f.get("length") or 0), index)
        f.update(m)

    for f in files:
        length = int(f.get("length") or parse_hsize(f.get("hsize") or ""))
        f["length"] = length
        special, reason = is_special_file(f.get("name") or "", length, large_bytes)
        f["special"] = special
        f["special_reason"] = reason

    # 同一附件可能被章节和资料各扫到一次
    seen: set[str] = set()
    uniq: list[dict] = []
    for f in files:
        key = f.get("objectid") or f.get("url") or ""
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        uniq.append(f)

    summary = summarize_check(uniq)
    return {"name": name, "courseid": info["courseid"],
            "local_dir": local_dir, "files": uniq, "summary": summary}


def summarize_check(files: list[dict]) -> dict:
    total = len(files)
    present = sum(1 for f in files if f.get("exists"))
    same = sum(1 for f in files if f.get("exists") and f.get("same"))
    missing = total - present
    updated = present - same
    special = [f for f in files if f.get("special")]
    return {
        "total": total, "present": present, "same": same,
        "updated": updated, "missing": missing, "special": len(special),
        "special_names": [f.get("name") for f in special],
        "missing_names": [f.get("name") for f in files if not f.get("exists")],
        "updated_names": [f.get("name") for f in files
                          if f.get("exists") and not f.get("same")],
    }


def describe_object(cookie: str | None, oid: str, timeout: int = 60) -> dict:
    """给裸 objectid 补上文件名 / 大小，方便在列表里显示。"""
    st = fetch_status(oid, cookie, timeout)
    return {
        "objectid": oid,
        "name": st.get("filename") or f"{oid}.bin",
        "hsize": human(int(st.get("length") or 0)),
        "length": st.get("length"),
    }


def parse_version(v: str) -> tuple[int, ...]:
    """把 v1.2.3 这种字符串拆成可比较的元组。"""
    nums = re.findall(r"\d+", v or "")
    return tuple(int(x) for x in nums[:3]) or (0,)


# 境内更新清单（latest.json）协议 —— 给境外不通的网络用。
# 托管在任意境内可达的静态地址即可，程序只发 GET，不需要服务端脚本。
# {
#   "version": "1.4.5", "tag": "v1.4.5", "name": "v1.4.5",
#   "body": "更新说明", "url": "https://.../releases", "published_at": "2026-11-14",
#   "assets": [{"name": "cx-downloader-v1.4.5-windows.exe",
#               "url": "https://.../xx.exe", "size": 15575745, "sha256": "..."}]
# }
# assets 里也可以直接写 URL 字符串。清单要和 GitHub Release 同步，否则会停在旧版本。


def parse_mirror_urls(text: str) -> list[str]:
    """设置里「境内更新源」的多行文本 → URL 列表（去注释、去重、只留 http/https）。"""
    out: list[str] = []
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if line.startswith(("http://", "https://")) and line not in out:
            out.append(line)
    return out


def _release_info(data: dict, current: str, source: str) -> dict | None:
    """GitHub Release JSON / 境内清单 → 统一的 info；不是新版本或格式不对返回 None。"""
    if not isinstance(data, dict):
        return None
    tag = str(data.get("tag_name") or data.get("tag") or "").strip()
    ver = str(data.get("version") or "").strip()
    if not ver and tag:
        ver = tag[1:] if tag[:1] in ("v", "V") else tag
    if not tag:
        tag = "v" + ver
    if not ver or parse_version(ver) <= parse_version(current):
        return None
    info = {
        "version": ver,
        "tag": tag,
        "name": data.get("name") or tag,
        "body": data.get("body") or "",
        "url": data.get("html_url") or data.get("url") or RELEASES_URL,
        "published_at": data.get("published_at") or "",
        "source": source,
    }
    urls: list[str] = []
    first: dict = {}
    for a in data.get("assets") or []:
        if isinstance(a, dict):
            u = str(a.get("browser_download_url") or a.get("url") or "").strip()
            if not u:
                continue
            n = str(a.get("name") or "").strip()
            if n and not n.lower().endswith(".exe"):
                continue
            if not first:
                first = a
            urls.append(u)
        elif isinstance(a, str) and a.strip():
            urls.append(a.strip())
    if urls:
        info["asset_urls"] = urls
        info["asset_url"] = urls[0]
        info["asset_size"] = int(first.get("size") or 0)
        info["asset_name"] = str(first.get("name") or "") or os.path.basename(urls[0])
        # GitHub 用 "sha256:xxx"，清单里通常直接给 hex，统一成前一种格式
        digest = str(first.get("digest") or first.get("sha256") or "").strip()
        info["asset_digest"] = (digest if digest.startswith("sha256:")
                                else "sha256:" + digest if digest else "")
    return info


def _html_release_info(final: str, current: str) -> dict | None:
    """github.com/.../releases/latest 的 302 目标 → info（拿不到资产详情）。"""
    try:
        tag = final.rstrip("/").rsplit("/", 1)[-1]
        ver = tag[1:] if tag[:1] in ("v", "V") else tag
        if not ver or parse_version(ver) <= parse_version(current):
            return None
        asset = f"cx-downloader-{tag}-windows.exe"
        url = (f"https://github.com/{GITHUB_REPO}/releases/download/"
               f"{tag}/{asset}")
        return {
            "version": ver,
            "tag": tag,
            "name": tag,
            "body": ("（GitHub API 和境内更新清单都不可用，只从 Release 页拿到版本号；"
                     "详细说明请点「浏览器下载」。）"),
            "url": final,
            "published_at": "",
            "source": "GitHub Release 页",
            "asset_urls": [url],
            "asset_url": url,
            "asset_size": 0,
            "asset_name": asset,
            "asset_digest": "",
        }
    except Exception:  # noqa: BLE001
        return None


def _fetch_release(kind: str, url: str, current: str,
                    timeout: int) -> dict | None:
    req = urllib.request.Request(
        url, headers={"User-Agent": UA,
                     "Accept": "application/json, text/plain, */*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if kind == "html":
            return _html_release_info(resp.geturl() or "", current)
        label = "境内更新清单" if kind == "manifest" else "GitHub API"
        return _release_info(json.load(resp), current, label)


# 配了境内清单时，先给它这么长的独占窗口再接受 GitHub 的结果。
# 校园网常见「api.github.com 能通，但资产 CDN 挂」：纯竞速会选到只有
# GitHub 单地址的 API 结果，境内镜像里的下载地址就用不上了。
MIRROR_PRIORITY_SEC = 1.5


def check_update(current: str | None = None, timeout: int = 8,
                 mirrors: list[str] | None = None) -> dict | None:
    """多源并行查更新，配了境内清单时它在优先窗口内先赢。

    源 = 用户配置的境内清单 URL（可多个）+ GitHub API + Release 页 302。
    境内清单在 `MIRROR_PRIORITY_SEC` 内先到就用它；窗口内没回（清单挂了）
    才放行为谁快谁赢，保证 GitHub 通的时候照样能更新。
    慢源都是 daemon 线程，拿到结果后主线程直接返回，不拖程序退出。
    """
    cur = current or __version__
    tasks: list[tuple[str, str]] = []
    for u in parse_mirror_urls("\n".join(mirrors or [])):
        tasks.append(("manifest", u))
    tasks.append(("api", f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"))
    tasks.append(("html", f"https://github.com/{GITHUB_REPO}/releases/latest"))

    results: queue.Queue[tuple[str, dict | None]] = queue.Queue()

    def worker(kind: str, url: str) -> None:
        try:
            info = _fetch_release(kind, url, cur, timeout)
            results.put(("ok" if info else "none", info))
        except Exception:  # noqa: BLE001
            results.put(("err", None))

    for kind, url in tasks:
        threading.Thread(target=worker, args=(kind, url), daemon=True).start()

    mirror_count = sum(1 for kind, _ in tasks if kind == "manifest")
    priority_until = time.time() + (MIRROR_PRIORITY_SEC if mirror_count else 0)
    deadline = time.time() + timeout + 2
    seen = 0
    fallback: dict | None = None
    while seen < len(tasks) and time.time() < deadline:
        try:
            status, info = results.get(timeout=0.2)
        except queue.Empty:
            # 窗口内 GitHub 先回了也先压着；窗口过了就用它
            if fallback is not None and time.time() >= priority_until:
                return fallback
            continue
        seen += 1
        if status != "ok":
            continue
        if not mirror_count or (info or {}).get("source") == "境内更新清单":
            return info
        if fallback is None:
            fallback = info
    return fallback


def check_exe_package(path: str, expect_sha256: str = "") -> tuple[bool, str]:
    """校验下载 / 手动导入的更新包是不是一个完整的 Windows exe。"""
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return False, f"读不到文件：{e}"
    if size < 1024 * 1024:
        return False, f"文件只有 {human(size)}，不像完整的 exe"
    try:
        with open(path, "rb") as f:
            if f.read(2) != b"MZ":
                return False, "文件不是有效的 Windows exe（开头不是 MZ）"
        if expect_sha256:
            want = expect_sha256.lower()
            if want.startswith("sha256:"):
                want = want.split(":", 1)[1]
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
            if h.hexdigest() != want:
                return False, "sha256 与发布信息不一致"
    except OSError as e:
        return False, f"校验时读文件失败：{e}"
    return True, ""


# ------------------------------------------------------------ 课程「资料」页
#
# 学生端的「资料」是另一套入口：/mooc2-ans/coursedata/stu-datalist
#   * 页面本身就是文件列表；进文件夹 = 同一个 URL 带 dataId=<文件夹id>&type=1；
#   * 每个文件行里藏着下载直链：
#       <li class="operate_down"><a href=".../coursedata/downloadData?dataId=..&classId=..">
# 所以递归展开文件夹就能把整门课的「资料」列全，和章节页互不影响。

COURSE_DATA_PAGE = "https://mooc2-ans.chaoxing.com/mooc2-ans/coursedata/stu-datalist"
_DATA_ROW_RE = re.compile(r'<ul[^>]*class="dataBody_td"[^>]*>', re.I)


def _parse_data_rows(html: str) -> list[dict]:
    """从「资料」页里抠出每一行（文件夹 / 文件 / 教师课件入口）。"""
    marks = list(_DATA_ROW_RE.finditer(html))
    out: list[dict] = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(html)
        body = html[m.end():end]
        attrs = {k.lower(): v for k, v in re.findall(r'(\w+)="([^"]*)"', m.group(0))}
        nm = re.search(r'class="rename_title[^"]*"[^>]*title="([^"]*)"', body)
        if not nm:
            nm = re.search(r'class="rename_title[^"]*"[^>]*>(.*?)</a>', body, re.S)
        name = _clean(re.sub(r"<[^>]+>", "", nm.group(1))) if nm else ""
        dl = re.search(r'class="operate_down".*?href=[\'"]([^\'"]+)[\'"]', body, re.S)
        sz = re.search(r'class="dataBody_size_stu"[^>]*>(.*?)</li>', body, re.S)
        out.append({
            "type": (attrs.get("type") or "").lower(),
            "dataid": attrs.get("id") or "",
            "name": name,
            "hsize": _clean(re.sub(r"<[^>]+>", "", sz.group(1))) if sz else "",
            "url": _html.unescape(dl.group(1)) if dl else "",
        })
    return out


def _fetch_data_page(cookie: str | None, courseid: str, clazzid: str, cpi: str,
                     enc: str, data_id: str | None, timeout: int,
                     referer: str) -> str:
    q = {"courseid": courseid, "clazzid": clazzid, "cpi": cpi, "ut": "s"}
    if data_id:
        q.update({"dataName": "", "dataId": data_id, "type": "1", "parent": "",
                  "enc": enc, "t": "0", "microTopicId": "0"})
    return _get_text(COURSE_DATA_PAGE + "?" + urllib.parse.urlencode(q),
                     cookie, timeout, referer=referer)


def scan_course_data(cookie: str | None, courseid: str, clazzid: str, cpi: str,
                     timeout: int = 60, progress=None, max_depth: int = 8) -> list[dict]:
    """递归展开课程「资料」页，返回全部可下载文件。

    每项: {objectid:'', dataid, name, hsize, url, folder, chapter, unit}
    """
    referer = f"{COURSE_DATA_PAGE}?courseid={courseid}"
    root = _fetch_data_page(cookie, courseid, clazzid, cpi, "", None, timeout, referer)
    m = re.search(r'<input[^>]*name="enc"[^>]*value="([^"]*)"', root)
    enc = m.group(1) if m else ""

    files: list[dict] = []
    seen: set[str] = set()

    def walk(rows: list[dict], path: str, depth: int) -> None:
        for row in rows:
            kind = row["type"]
            if kind == "tch-courseware":
                # 「教师课件」只是跳到章节课件的入口，章节那边已经扫过了
                continue
            if kind == "afolder":
                did = row["dataid"]
                if not did or did in seen or depth >= max_depth:
                    continue
                seen.add(did)
                sub_path = f"{path}{row['name']} / "
                if progress:
                    progress(f"资料 / {sub_path.rstrip(' /')}")
                try:
                    sub = _fetch_data_page(cookie, courseid, clazzid, cpi, enc,
                                           did, timeout, referer)
                except Exception as e:  # noqa: BLE001
                    if progress:
                        progress(f"  文件夹读取失败：{row['name']}（{e}）")
                    continue
                walk(_parse_data_rows(sub), sub_path, depth + 1)
            elif row["url"]:
                row["objectid"] = ""
                row["source"] = "资料"
                row["folder"] = path.rstrip(" /")
                row["chapter"] = row["folder"]
                row["unit"] = ""
                files.append(row)

    walk(_parse_data_rows(root), "", 0)
    return files


def download_data_one(url: str, outdir: str, name_hint: str | None,
                      cookie: str | None, timeout: int = 60,
                      overwrite: bool = False) -> dict:
    """下载「资料」页里的一个文件。返回结构与 process_one 一致。"""
    res = {"objectid": url, "ok": False, "files": [], "error": None}
    try:
        fname = resolve_name(name_hint, name_hint, name_hint or "data")
        dest = os.path.join(outdir, fname)
        if not overwrite and os.path.exists(dest):
            total, _ = remote_probe(url, cookie, timeout)
            if total and os.path.getsize(dest) == total:
                res["files"].append((dest, total, "已存在，跳过"))
                res["ok"] = True
                res["filename"] = fname
                return res
        size = download(url, dest, timeout, cookie=cookie)
        res["files"].append((dest, size, "下载完成"))
        res["ok"] = True
        res["filename"] = fname
    except Exception as e:  # noqa: BLE001
        res["error"] = str(e)
    return res


def collect_inputs(args) -> str:
    chunks: list[str] = []
    if args.files:
        for path in args.files:
            with open(path, encoding="utf-8-sig", errors="replace") as f:
                chunks.append(f.read())
    if args.links:
        chunks.append("\n".join(args.links))
    if args.clip:
        try:
            import tkinter  # noqa: PLC0415

            root = tkinter.Tk()
            root.withdraw()
            chunks.append(root.clipboard_get())
            root.destroy()
        except Exception as e:  # noqa: BLE001
            log(f"[警告] 读剪贴板失败: {e}")
    if not chunks and not sys.stdin.isatty():
        chunks.append(sys.stdin.read())
    return "\n".join(chunks)


def main() -> int:
    p = argparse.ArgumentParser(
        description="超星/学习通 预览文件批量下载器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法\n----")[-1],
    )
    p.add_argument("links", nargs="*", help="预览页链接或 32 位 objectid")
    p.add_argument("-f", "--files", nargs="*", help="存放链接的文本文件")
    p.add_argument("--clip", action="store_true", help="从剪贴板读取链接")
    p.add_argument("-o", "--out", default="cx_download", help="保存目录（默认 ./cx_download）")
    p.add_argument("--pdf", action="store_true", help="额外下载 PDF")
    p.add_argument("--pdf-only", action="store_true", help="只下载 PDF")
    p.add_argument("--pdf-fallback", action="store_true",
                   help="配合 --pdf-only：没有 PDF 版的格式（csv/py/xlsx…）改下原件")
    p.add_argument("--video", action="store_true",
                   help="视频任务点也下载（默认取转码好的 mp4，几十 MB）")
    p.add_argument("--video-original", action="store_true",
                   help="配合 --video：下原件而不是转码 mp4（动辄几百 MB）")
    p.add_argument("--jobs", type=int, default=4, help="并发数（默认 4）")
    p.add_argument("--cookie", default=os.environ.get("CX_COOKIE"), help="可选 Cookie")
    p.add_argument("--overwrite", action="store_true", help="强制重下已存在的文件")
    p.add_argument("--no-data", action="store_true", help="课程页只扫章节任务点，不扫「资料」")
    p.add_argument("--timeout", type=int, default=60, help="单请求超时秒数（默认 60）")
    p.add_argument("--check-local", action="store_true",
                   help="只检查：按本地课程目录递归比对是否已有，不下载")
    p.add_argument("--local-root", default=None,
                   help="本地课程根目录（默认同 --out）")
    p.add_argument("--download-missing", action="store_true",
                   help="配合 --check-local：下载缺失的普通文件")
    p.add_argument("--include-special", action="store_true",
                   help="配合 --check-local：视频/安装包/压缩包/大文件也一起下载")
    p.add_argument("--large-mb", type=int, default=100,
                   help="大文件阈值 MB（默认 100）")
    p.add_argument("--report-json", default=None,
                   help="配合 --check-local：把检查结果写成 JSON")
    args = p.parse_args()

    raw = collect_inputs(args)
    outdir = os.path.abspath(args.out)
    os.makedirs(outdir, exist_ok=True)

    # 登录态：优先用 --cookie，否则直接借本机 Firefox 的 cookie（课程页扫描必需）
    cookie = args.cookie or firefox_cookie_header() or None

    # 待下载任务: (类型, 载荷, 保存目录, 文件名提示)
    #   类型 "oid"  → 载荷是 objectid，走 process_one
    #   类型 "data" → 载荷是「资料」页的下载直链，走 download_data_one
    jobs: list[tuple[str, str, str, str | None]] = [
        ("oid", oid, outdir, None) for oid in extract_objectids(raw)
    ]

    course_urls = extract_course_urls(raw)
    if course_urls and not cookie:
        log("[警告] 没读到登录态，课程页扫描会拿到登录页。两种办法：")
        log("        ① 在 Firefox 里登录一次学习通（脚本会自动读它的 cookie）")
        log("        ② 用 --cookie 手动指定浏览器里复制出来的 Cookie 请求头")

    if args.check_local:
        if not course_urls:
            log("--check-local 需要课程页链接（URL 里要有 courseid / clazzid）")
            return 2
        local_root = os.path.abspath(args.local_root or args.out)
        large = max(1, args.large_mb) * 1024 * 1024
        reports = []
        rc = 0
        for u in course_urls:
            log(f"\n[检查] {u}")
            try:
                rep = check_local_course(cookie, u, local_root, args.timeout,
                                         progress=lambda m: log("  " + m),
                                         large_bytes=large)
            except Exception as e:  # noqa: BLE001
                log(f"  检查失败：{e}")
                rc = 1
                continue
            reports.append(rep)
            s = rep["summary"]
            log(f"  课程：{rep['name']}")
            log(f"  本地目录：{rep['local_dir'] or '（无）'}")
            log(f"  平台 {s['total']} 个；已有 {s['present']}（内容相同 {s['same']}）；"
                f"缺失 {s['missing']}；特殊/大文件 {s['special']}")
            for f in rep["files"]:
                if f.get("exists"):
                    continue
                tag = "特殊" if f.get("special") else "缺失"
                extra = f" · {f.get('special_reason')}" if f.get("special") else ""
                log(f"    [{tag}] {f.get('name')}（{f.get('hsize') or ''}）{extra}")
            if s["special"] and not args.include_special:
                log("  提示：特殊/大文件默认不下载；要下请加 --include-special")
        if args.report_json:
            with open(args.report_json, "w", encoding="utf-8") as fp:
                json.dump(reports, fp, ensure_ascii=False, indent=1)
            log(f"\n检查结果已写入 {args.report_json}")
        if not args.download_missing:
            return rc
        jobs = []
        for rep in reports:
            target = rep["local_dir"] or os.path.join(
                local_root, sanitize(rep["name"], rep["courseid"]))
            for f in rep["files"]:
                if f.get("exists"):
                    continue
                if f.get("special") and not args.include_special:
                    continue
                if f.get("url"):
                    jobs.append(("data", f["url"], target, f.get("name")))
                elif f.get("objectid"):
                    jobs.append(("oid", f["objectid"], target, f.get("name")))
        if not jobs:
            log("没有需要下载的缺失文件。")
            return rc

    for u in ([] if args.check_local else course_urls):
        info = parse_course_url(u)
        if not info:
            continue
        log(f"[课程] 扫描 {u}")
        name = fetch_course_name(cookie, u, args.timeout) or f"课程{info['courseid']}"
        sub = os.path.join(outdir, sanitize(name, info["courseid"]))
        os.makedirs(sub, exist_ok=True)

        # ① 章节页的任务点附件
        try:
            sc = scan_course(cookie, u, args.timeout, progress=lambda m: log("  " + m))
            log(f"  → 章节任务点 {len(sc['files'])} 个")
            for f in sc["files"]:
                jobs.append(("oid", f["objectid"], sub, f.get("name") or None))
        except Exception as e:  # noqa: BLE001
            log(f"  章节扫描失败：{e}")

        # ② 「资料」页的文件
        if not args.no_data:
            try:
                dfiles = scan_course_data(
                    cookie, info["courseid"], info["clazzid"], info["cpi"],
                    args.timeout, progress=lambda m: log("  " + m))
                log(f"  → 课程资料 {len(dfiles)} 个")
                for f in dfiles:
                    jobs.append(("data", f["url"], sub, f.get("name") or None))
            except Exception as e:  # noqa: BLE001
                log(f"  资料扫描失败：{e}")
        log(f"  保存到 {sub}")

    if not jobs:
        log("没找到任何可下载内容。给预览页链接 / objectid，或课程页链接，例如：")
        log("  python cx_download.py \"https://mooc1.chaoxing.com/ananas/modules/pub/preview.html?objectid=xxx\"")
        log("  python cx_download.py \"https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/stu?courseid=..&clazzid=..\"")
        return 2

    want_original = not args.pdf_only
    want_pdf = args.pdf or args.pdf_only

    log(f"\n共 {len(jobs)} 个文件")
    mode_txt = "仅PDF" if args.pdf_only else ("原件+PDF" if args.pdf else "仅原件")
    if args.pdf_only and args.pdf_fallback:
        mode_txt += "（无 PDF 版时下原件）"
    log(f"模式: {mode_txt}, 并发 {args.jobs}\n")

    ok = fail = skip = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
        futs = {}
        for kind, payload, job_dir, name_hint in jobs:
            if kind == "data":
                fut = ex.submit(download_data_one, payload, job_dir, name_hint,
                                cookie, args.timeout, args.overwrite)
            else:
                fut = ex.submit(process_one, payload, job_dir, want_original, want_pdf,
                                cookie, args.timeout, args.overwrite, name_hint,
                                args.pdf_fallback, args.video, args.video_original)
            futs[fut] = payload
        for fut in as_completed(futs):
            r = fut.result()
            if r.get("skip"):
                skip += 1
                log(f"[跳过] {r.get('filename') or r['objectid']}　（{r.get('skip_reason', '不支持')}）")
            elif r["ok"]:
                ok += 1
                extra = f"  ({r['pagenum']}p)" if r.get("pagenum") else ""
                log(f"[OK] {r.get('filename')}{extra}")
                for dest, size, note in r["files"]:
                    log(f"     {note}: {os.path.basename(dest)}  {human(size)}")
            else:
                fail += 1
                log(f"[失败] {r.get('filename') or r['objectid']}\n       {r['error']}")

    log(f"\n完成: 成功 {ok} / 跳过 {skip} / 失败 {fail}，耗时 {time.time() - t0:.1f}s")
    log(f"目录: {outdir}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)









