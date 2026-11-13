#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""冻结后自检 —— 在打包好的 exe 里运行，验证「真正出货的那个包」。

为什么放在 exe 里而不是另编一个自检 exe：
另编的自检 exe 有自己的打包参数，验不了主 exe 的依赖收集情况。放进主 exe 后，
`超星文件下载器.exe --selftest` 验的就是真正要发给别人的那个二进制。

用法：
    超星文件下载器.exe --selftest                 # 用内置样例 objectid
    超星文件下载器.exe --selftest <oid> [<oid>…]  # 指定 objectid
    超星文件下载器.exe --selftest --no-network    # 只验打包完整性，不联网
    python selftest.py                            # 源码态直接跑

报告写到 %TEMP%\\cx_download_selftest.txt，退出码 0=通过 / 1=失败。
（--windowed 的 exe 没有 stdout，所以必须落盘报告，否则构建脚本无从判断。）
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

# 内置样例：学习通公开预览文件，不依赖登录。若哪天失效，
# 用命令行参数或环境变量 CX_SELFTEST_OIDS 覆盖即可。
DEFAULT_OIDS = [
    "e934d91c35851d5bcbf3338d41648aea",  # 公开预览样例 1
    "39a58c0915664f14110611625ef5d07b",  # 公开预览样例 2
]

# 当前 core 必须提供的符号，用来确认打进包的是「新版本」而不是陈旧的 core
REQUIRED_CORE_ATTRS = [
    "process_one", "fetch_status", "extract_objectids",
    "scan_course", "scan_course_data", "download_data_one",
]

DEFAULT_REPORT = os.path.join(tempfile.gettempdir(), "cx_download_selftest.txt")


class Report:
    def __init__(self):
        self.lines: list[str] = []
        self.failed = 0

    def ok(self, msg: str):
        self.lines.append(f"[PASS] {msg}")
        self._echo(f"[PASS] {msg}")

    def bad(self, msg: str):
        self.lines.append(f"[FAIL] {msg}")
        self._echo(f"[FAIL] {msg}")
        self.failed += 1

    def info(self, msg: str):
        self.lines.append(f"       {msg}")
        self._echo(f"       {msg}")

    @staticmethod
    def _echo(msg: str):
        # --windowed 的 exe 里 sys.stdout 可能是 None，print 会炸
        try:
            print(msg, flush=True)
        except Exception:  # noqa: BLE001
            pass


def _check_bundle(rep: Report):
    """① 打包完整性：core 是否随包、是不是新版、CTk 资源是否在。"""
    frozen = getattr(sys, "frozen", False)
    rep.info(f"frozen = {frozen}, meipass = {getattr(sys, '_MEIPASS', None)}")

    # core 模块（两文件结构的关键：cx_download.py 必须被一起收进来）
    try:
        import cx_download as core  # noqa: PLC0415

        missing = [a for a in REQUIRED_CORE_ATTRS if not hasattr(core, a)]
        if missing:
            rep.bad(f"cx_download 缺少符号 {missing} —— 打进包的很可能是旧版 core")
        else:
            rep.ok(f"cx_download 已随包，且含新符号（{', '.join(REQUIRED_CORE_ATTRS)}）")
        rep.info(f"cx_download.__file__ = {getattr(core, '__file__', '?')}")
    except Exception as e:  # noqa: BLE001
        rep.bad(f"import cx_download 失败: {e!r}")

    # customtkinter 的数据文件（主题 JSON / 字体 / 图标）
    try:
        import customtkinter  # noqa: PLC0415

        base = os.path.dirname(customtkinter.__file__)
        need = [
            os.path.join("assets", "themes", "blue.json"),
            os.path.join("assets", "themes", "dark-blue.json"),
            os.path.join("assets", "fonts", "CustomTkinter_shapes_font.otf"),
        ]
        missing = [p for p in need if not os.path.exists(os.path.join(base, p))]
        if missing:
            rep.bad(f"customtkinter 资源缺失 {missing} —— 忘了 --collect-all？会白屏/全黑")
        else:
            rep.ok("customtkinter 主题/字体资源齐全")
        rep.info(f"customtkinter {customtkinter.__version__} @ {base}")
    except Exception as e:  # noqa: BLE001
        rep.bad(f"customtkinter 检查失败: {e!r}")

    # GUI 模块本身能否导入（顺带确认它依赖的 tkinter 等都在）
    try:
        import cx_download_gui  # noqa: F401, PLC0415

        rep.ok("cx_download_gui 可导入")
    except Exception as e:  # noqa: BLE001
        rep.bad(f"import cx_download_gui 失败: {e!r}")


def _check_network(rep: Report, objectids: list[str], timeout: int = 60):
    """② 联网 + 写盘：HTTPS 走系统证书库，冻结后不一定还灵，必须实测。"""
    import cx_download as core  # noqa: PLC0415

    out = tempfile.mkdtemp(prefix="cxselftest_")
    rep.info(f"下载到 {out}")

    for oid in objectids:
        try:
            st = core.fetch_status(oid, None, timeout)
        except Exception as e:  # noqa: BLE001
            rep.bad(f"{oid} 状态接口失败: {e!r}（样例可能已失效，可用参数覆盖）")
            continue
        if st.get("status") != "success":
            rep.bad(f"{oid} 接口返回 status={st.get('status')!r}")
            continue

        r = core.process_one(oid, out, True, True, None, timeout, True)
        if not r.get("ok"):
            rep.bad(f"{oid} 下载失败: {r.get('error')}")
            continue

        good = True
        for dest, size, _note in r["files"]:
            if not os.path.exists(dest) or os.path.getsize(dest) == 0:
                good = False
            rep.info(f"{os.path.basename(dest)}  {size} B")
        if good:
            rep.ok(f"{r.get('filename')} 原件+PDF 下载并落盘正常")
        else:
            rep.bad(f"{oid} 下载后文件缺失或为空")


def run(objectids: list[str] | None = None, network: bool = True,
        report_path: str | None = None) -> bool:
    t0 = time.time()
    rep = Report()

    env = os.environ.get("CX_SELFTEST_OIDS", "").strip()
    oids = objectids or ([o for o in env.replace(",", " ").split() if o] or DEFAULT_OIDS)

    rep.info("=" * 60)
    rep.info(f"cx-download 冻结自检  python {sys.version.split()[0]}")
    rep.info("=" * 60)

    _check_bundle(rep)
    if network:
        _check_network(rep, oids)
    else:
        rep.info("跳过联网检查（--no-network）")

    passed = rep.failed == 0
    rep.info("-" * 60)
    rep.info(f"SELFTEST {'PASS' if passed else 'FAIL'}  耗时 {time.time() - t0:.1f}s")
    rep._echo(f"SELFTEST {'PASS' if passed else 'FAIL'}")

    path = report_path or DEFAULT_REPORT
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(rep.lines) + "\n")
        rep._echo(f"报告: {path}")
    except Exception as e:  # noqa: BLE001
        rep._echo(f"报告写入失败: {e!r}")

    return passed


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    sys.exit(0 if run(args, network="--no-network" not in sys.argv) else 1)
