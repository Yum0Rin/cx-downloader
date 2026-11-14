#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从 GitHub Release 生成境内更新清单 latest.json（cx-downloader 多源更新用）。

清单协议见 README「更新清单协议」。把生成的文件推到 Gitee 公开仓库，
再把该文件的 raw 地址填进「设置 → 关于 → 境内更新源」。

用法：
    python packaging/make_update_manifest.py
    python packaging/make_update_manifest.py --asset-base "https://gitee.com/<owner>/<repo>/releases/download/{tag}"
    python packaging/make_update_manifest.py --tag v1.4.5 --out packaging/latest.json
    python packaging/make_update_manifest.py --release-json release.json   # 离线/CI

注意：
- Gitee 仓库 raw 只适合放 latest.json 这种小文件。exe 有 15MB，
  公开仓库 raw 超过 10MB 就要登录（403），所以 exe 必须放 Gitee Release 附件，
  用 --asset-base 指过去，别放仓库文件里。
- 给了 --asset-base 时，同一个 exe 会输出两个 assets 条目：境内附件在前、
  GitHub 原地址在后。下载器会把它们收进 asset_urls 按顺序逐个回退。
- Gitee raw 会 302 到 raw.giteeusercontent.com，且有 60~300 秒缓存，
  发新版后别急着立刻检查，或换个文件名绕开缓存。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

REPO = "Yum0Rin/cx-downloader"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")
HERE = os.path.dirname(os.path.abspath(__file__))


def fetch_release(tag: str | None, timeout: int) -> dict:
    url = (f"https://api.github.com/repos/{REPO}/releases/tags/{tag}"
           if tag else f"https://api.github.com/repos/{REPO}/releases/latest")
    req = urllib.request.Request(
        url, headers={"User-Agent": UA,
                     "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def build_manifest(rel: dict, asset_base: str = "") -> dict:
    tag = str(rel.get("tag_name") or "").strip()
    version = tag[1:] if tag[:1] in ("v", "V") else tag
    base = (asset_base.replace("{tag}", tag)
                       .replace("{version}", version)).rstrip("/")
    assets = []
    for a in rel.get("assets") or []:
        name = str(a.get("name") or "").strip()
        gh = str(a.get("browser_download_url") or "").strip()
        if not gh or not name.lower().endswith(".exe"):
            continue
        digest = str(a.get("digest") or "").strip()
        sha = digest.split(":", 1)[1] if digest.startswith("sha256:") else ""
        common = {"name": name, "size": int(a.get("size") or 0)}
        if sha:
            common["sha256"] = sha
        urls = ([base + "/" + name] if base else []) + [gh]
        for u in urls:
            entry = dict(common)
            entry["url"] = u
            assets.append(entry)
    return {
        "version": version,
        "tag": tag,
        "name": rel.get("name") or tag,
        "body": rel.get("body") or "",
        "url": rel.get("html_url") or "",
        "published_at": rel.get("published_at") or "",
        "assets": assets,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="生成境内更新清单 latest.json")
    p.add_argument("--tag", help="指定 Release tag，默认最新")
    p.add_argument("--release-json", default="",
                    help="读本地 Release JSON（GitHub API 同格式），不联网；"
                         "本机 GitHub API 抖动或 CI 里用这个")
    p.add_argument("--asset-base", default="",
                    help="境内 exe 附件基址（Gitee Release 附件），"
                         "支持 {tag} / {version} 占位符")
    p.add_argument("--out", default=os.path.join(HERE, "latest.json"))
    p.add_argument("--timeout", type=int, default=15)
    args = p.parse_args()

    try:
        if args.release_json:
            with open(args.release_json, encoding="utf-8") as f:
                rel = json.load(f)
        else:
            rel = fetch_release(args.tag, args.timeout)
    except Exception as e:  # noqa: BLE001
        print(f"取 Release 失败：{e}", file=sys.stderr)
        return 1
    manifest = build_manifest(rel, args.asset_base)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"已生成 {args.out}")
    print(f"  version = {manifest['version']}  tag = {manifest['tag']}")
    for a in manifest["assets"]:
        print(f"  {a['name']}  {a['size']} B  {a['url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
