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

    # 常用选项
    -o, --out DIR      保存目录（默认 ./cx_download）
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
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

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

    注意: d0.cldisk.com 的 download 直链会校验 Referer，缺了会 403。
    cookie 只对 chaoxing 自己的域名传（云盘直链不需要，也不该把课程 cookie 发过去）。
    """
    tmp = dest + ".tmp"
    headers = {"User-Agent": UA, "Referer": "https://mooc1.chaoxing.com/"}
    if cookie:
        headers["Cookie"] = cookie
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as f:
                total = 0
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    total += len(chunk)
            os.replace(tmp, dest)
            return total
        except Exception as e:  # noqa: BLE001
            last = e
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"下载失败: {last}")


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}GB"


def process_one(
    oid: str,
    outdir: str,
    want_original: bool,
    want_pdf: bool,
    cookie: str | None,
    timeout: int,
    overwrite: bool,
    name_hint: str | None = None,
) -> dict:
    res = {"objectid": oid, "ok": False, "files": [], "error": None}
    try:
        st = fetch_status(oid, cookie, timeout)
        if st.get("status") != "success":
            raise RuntimeError(f"接口返回 status={st.get('status')!r}")

        fname = resolve_name(name_hint, st.get("filename"), oid)
        stem, ext = split_ext(fname)

        jobs: list[tuple[str, str]] = []
        if want_original and st.get("download"):
            jobs.append((st["download"], os.path.join(outdir, fname)))
        if want_pdf and st.get("pdf"):
            jobs.append((st["pdf"], os.path.join(outdir, (sanitize(stem, oid) or oid) + ".pdf")))
        if not jobs:
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
            size = download(url, dest, timeout)
            res["files"].append((dest, size, "下载完成"))

        res["ok"] = True
        res["filename"] = fname
        res["pagenum"] = st.get("pagenum")
    except Exception as e:  # noqa: BLE001
        res["error"] = str(e)
    return res


# ============================================================ 课程页扫描
#
# 思路：学习通的「章节」页把每个任务点渲染成 <iframe class="ans-attach-online"
# data="{objectid,name,type,size,...}">。所以只要拿到章节列表，逐章请求
#     https://mooc1.chaoxing.com/mooc-ans/knowledge/cards?...&knowledgeid=<章节id>
# 把里面的 ans-attach-online 抠出来，就等于把整门课的附件列全了。
#
# 这一步需要登录态，直接复用本机 Firefox 的 cookies.sqlite（明文存储），
# 免去手动复制 Cookie。拿到 objectid 之后走的是原来那套 /ananas/status/ 直链。


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
                    {"host": h, "name": n, "value": v or "", "path": p or "/"}
                    for h, n, v, p in con.execute(
                        "select host, name, value, path from moz_cookies "
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


def firefox_cookie_header(profile: str | None = None) -> str:
    """拼成 Cookie: 请求头。同名 cookie 只取第一个。"""
    _, rows = load_firefox_cookies(profile)
    seen: set[str] = set()
    parts: list[str] = []
    for r in rows:
        if r["name"] in seen:
            continue
        seen.add(r["name"])
        parts.append(f'{r["name"]}={r["value"].strip()}')
    return "; ".join(parts)


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


def fetch_chapter_attachments(cookie: str | None, courseid: str, clazzid: str,
                              cpi: str, kid: str, timeout: int = 60) -> list[dict]:
    """取某一章节下的所有附件（任务点里的文档/PPT/PDF 等）。"""
    q = urllib.parse.urlencode({
        "clazzid": clazzid, "courseid": courseid, "knowledgeid": kid, "num": "0",
        "ut": "s", "cpi": cpi, "mooc2": "1", "isMicroCourse": "false",
        "editorPreview": "0",
    })
    url = "https://mooc1.chaoxing.com/mooc-ans/knowledge/cards?" + q
    page = _get_text(url, cookie, timeout, referer="https://mooc1.chaoxing.com/")

    out: list[dict] = []
    for m in re.finditer(r'<[^>]*class="[^"]*ans-attach-online[^"]*"[^>]*>', page):
        tag = m.group(0)
        mo = re.search(r'objectid="([0-9a-fA-F]{32})"', tag)
        md = re.search(r'data="([^"]*)"', tag)
        info: dict = {}
        if md:
            try:
                info = json.loads(_html.unescape(md.group(1)))
            except Exception:  # noqa: BLE001
                info = {}
        oid = (mo.group(1) if mo else info.get("objectid") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{32}", oid):
            continue
        out.append({
            "objectid": oid,
            "name": _clean(info.get("name") or ""),
            "type": info.get("type") or "",
            "hsize": info.get("hsize") or "",
            "jobid": str(info.get("jobid") or ""),
        })
    return out


def scan_course(cookie: str | None, url: str, timeout: int = 60,
                progress=None) -> dict:
    """扫一门课，返回 {name, courseid, files:[{objectid,name,chapter,unit,...}]}。"""
    info = parse_course_url(url)
    if not info:
        raise RuntimeError("不是可识别的课程页链接（URL 里需要 courseid 与 clazzid）")

    def say(msg: str) -> None:
        if progress:
            progress(msg)

    name = fetch_course_name(cookie, url, timeout) or f"超星课程-{info['courseid']}"
    say(f"课程：{name}")
    chapters = fetch_course_chapters(
        cookie, info["courseid"], info["clazzid"], info["cpi"], timeout
    )
    say(f"章节：{len(chapters)} 个，开始逐章找附件…")

    files: list[dict] = []
    for i, ch in enumerate(chapters, 1):
        try:
            atts = fetch_chapter_attachments(
                cookie, info["courseid"], info["clazzid"], info["cpi"],
                ch["kid"], timeout,
            )
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
    return {"name": name, "courseid": info["courseid"], "files": uniq}


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


def resolve_course_files(cookie: str | None, files: list[dict], outdir: str,
                         timeout: int = 60, progress=None,
                         workers: int = 6) -> list[dict]:
    """给扫描结果补上「落到哪个路径 / 本地是否已有 / 是否同一份」。

    只有本地**已经存在同名文件**时才会去请求服务端比对，全新文件零额外请求。
    """
    pending: list[dict] = []
    for f in files:
        f["outdir"] = outdir
        key = f.get("objectid") or f.get("dataid") or "file"
        f["target"] = predict_name(f.get("name") or "", key)
        f["path"] = os.path.join(outdir, f["target"]) if f["target"] else ""
        f["exists"] = bool(f["path"]) and os.path.exists(f["path"])
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


def describe_object(cookie: str | None, oid: str, timeout: int = 60) -> dict:
    """给裸 objectid 补上文件名 / 大小，方便在列表里显示。"""
    st = fetch_status(oid, cookie, timeout)
    return {
        "objectid": oid,
        "name": st.get("filename") or f"{oid}.bin",
        "hsize": human(int(st.get("length") or 0)),
        "length": st.get("length"),
    }


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
    p.add_argument("--jobs", type=int, default=4, help="并发数（默认 4）")
    p.add_argument("--cookie", default=os.environ.get("CX_COOKIE"), help="可选 Cookie")
    p.add_argument("--overwrite", action="store_true", help="强制重下已存在的文件")
    p.add_argument("--no-data", action="store_true", help="课程页只扫章节任务点，不扫「资料」")
    p.add_argument("--timeout", type=int, default=60, help="单请求超时秒数（默认 60）")
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

    for u in course_urls:
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
    log(f"模式: {'仅PDF' if args.pdf_only else ('原件+PDF' if args.pdf else '仅原件')}, 并发 {args.jobs}\n")

    ok = fail = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
        futs = {}
        for kind, payload, job_dir, name_hint in jobs:
            if kind == "data":
                fut = ex.submit(download_data_one, payload, job_dir, name_hint,
                                cookie, args.timeout, args.overwrite)
            else:
                fut = ex.submit(process_one, payload, job_dir, want_original, want_pdf,
                                cookie, args.timeout, args.overwrite, name_hint)
            futs[fut] = payload
        for fut in as_completed(futs):
            r = fut.result()
            if r["ok"]:
                ok += 1
                extra = f"  ({r['pagenum']}p)" if r.get("pagenum") else ""
                log(f"[OK] {r.get('filename')}{extra}")
                for dest, size, note in r["files"]:
                    log(f"     {note}: {os.path.basename(dest)}  {human(size)}")
            else:
                fail += 1
                log(f"[失败] {r['objectid']}\n       {r['error']}")

    log(f"\n完成: 成功 {ok} / 失败 {fail}，耗时 {time.time() - t0:.1f}s")
    log(f"目录: {outdir}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
