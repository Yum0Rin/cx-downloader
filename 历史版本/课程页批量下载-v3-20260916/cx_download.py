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

用法
----
    # 直接把链接当参数
    python cx_download.py "https://mooc1.chaoxing.com/ananas/modules/pub/preview.html?objectid=xxx" ...

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
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

OBJECTID_RE = re.compile(r"(?:objectid=)?([0-9a-fA-F]{32})")
ILLEGAL_RE = re.compile(r'[\\/:*?"<>|\r\n\t]')


def log(msg: str) -> None:
    print(msg, flush=True)


def extract_objectids(text: str) -> list[str]:
    """从任意文本里抓 objectid，保序去重。"""
    seen: dict[str, None] = {}
    for m in OBJECTID_RE.finditer(text or ""):
        seen.setdefault(m.group(1).lower(), None)
    return list(seen)


def sanitize(name: str, fallback: str) -> str:
    name = ILLEGAL_RE.sub("_", (name or "").strip()).strip(" .")
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


def download(url: str, dest: str, timeout: int, retries: int = 3) -> int:
    """流式下载到 dest.tmp 再改名，避免半截文件。返回字节数。

    注意: d0.cldisk.com 的 download 直链会校验 Referer，缺了会 403。
    """
    tmp = dest + ".tmp"
    headers = {"User-Agent": UA, "Referer": "https://mooc1.chaoxing.com/"}
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
) -> dict:
    res = {"objectid": oid, "ok": False, "files": [], "error": None}
    try:
        st = fetch_status(oid, cookie, timeout)
        if st.get("status") != "success":
            raise RuntimeError(f"接口返回 status={st.get('status')!r}")

        fname = st.get("filename") or f"{oid}.bin"
        stem, ext = split_ext(fname)
        base = sanitize(stem, oid) or oid

        jobs: list[tuple[str, str]] = []
        if want_original and st.get("download"):
            jobs.append((st["download"], os.path.join(outdir, base + ext)))
        if want_pdf and st.get("pdf"):
            jobs.append((st["pdf"], os.path.join(outdir, base + ".pdf")))
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
    p.add_argument("--timeout", type=int, default=60, help="单请求超时秒数（默认 60）")
    args = p.parse_args()

    raw = collect_inputs(args)
    oids = extract_objectids(raw)
    if not oids:
        log("没找到任何 objectid。给链接或 32 位 id，例如：")
        log("  python cx_download.py \"https://mooc1.chaoxing.com/ananas/modules/pub/preview.html?objectid=xxx\"")
        return 2

    outdir = os.path.abspath(args.out)
    os.makedirs(outdir, exist_ok=True)

    want_original = not args.pdf_only
    want_pdf = args.pdf or args.pdf_only

    log(f"共 {len(oids)} 个文件 → {outdir}")
    log(f"模式: {'仅PDF' if args.pdf_only else ('原件+PDF' if args.pdf else '仅原件')}, 并发 {args.jobs}\n")

    ok = fail = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
        futs = {
            ex.submit(
                process_one,
                oid,
                outdir,
                want_original,
                want_pdf,
                args.cookie,
                args.timeout,
                args.overwrite,
            ): oid
            for oid in oids
        }
        for fut in as_completed(futs):
            r = fut.result()
            if r["ok"]:
                ok += 1
                log(f"[OK] {r.get('filename')}  ({r.get('pagenum')}p)")
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
