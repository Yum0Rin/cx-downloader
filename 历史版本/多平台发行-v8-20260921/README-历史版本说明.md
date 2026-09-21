# 历史版本说明：多平台发行（v8）

- **改动名**：多平台发行-v8
- **日期**：2026-09-21
- **目录**：`历史版本/多平台发行-v8-20260921/`
- **本目录内容**：本次改动**后**的全套产物 —— `cx_download.py`、`cx_download_gui.py`、
  `selftest.py`、`cx-download.spec`、`build.sh`（新增）、`build.ps1`、`make_icon.py`、
  `README.md`、`packaging/cx-downloader.desktop`（新增）、`workflows/build.yml`（新，原 `build-exe.yml`）
- **改动前那一版**：`历史版本/登录态引导-v7-20260917/`（源码）＋ git `9e9fcf0`

---

## 1. 为什么改

此前整套东西是**按 Windows 一条路写死**的，虽然核心下载逻辑本来就是跨平台的，但：

- 只有 Windows 能自动读登录态：Firefox 路径写死 `%APPDATA%\Mozilla\Firefox`；
- 界面上有几处裸调 Windows API（`ctypes.windll` 取屏幕尺寸、DWM 染标题栏）；
- 「打开目录」用的是 Windows 专有的 `os.startfile`，Linux 上直接抛 `AttributeError`；
- 字体写死 `Microsoft YaHei UI` / `Consolas`，非 Windows 上看不到，只能靠 Tk 静默回退；
- 打包/发版只有 `build.ps1` + `windows-latest`，非 Windows 用户拿不到二进制。

目标：**同一份源码三平台都能跑，且三平台都能出免安装包**。

---

## 2. 改了哪些文件 / 核心差异

### 2.1 `cx_download.py`（登录态来源跨平台）

- 新增 `firefox_roots()`：按平台给候选根目录
  - Windows：`%APPDATA%\Mozilla\Firefox`
  - macOS：`~/Library/Application Support/Firefox`
  - Linux：`~/.mozilla/firefox` 和 `~/snap/firefox/common/.mozilla/firefox`（snap 版）
- `firefox_root()` 改为「返回实际存在的那个」；`find_firefox_profiles()` 遍历所有候选根，
  每根先读 `profiles.ini`，再兜底扫目录；兜底只收**带 `prefs.js` 的真 profile**
  （否则 Linux 上会把 `Crash Reports` / `Pending Pings` / `Profile Groups` 一起收进来）。
- profiles.ini 的解析逻辑本来就跟平台无关，三平台通用。

### 2.2 `cx_download_gui.py`（界面跨平台）

| 位置 | 改动前 | 改动后 |
| --- | --- | --- |
| 屏幕尺寸 | `ctypes.windll.user32.GetSystemMetrics` | 新增 `_screen_size()`：Windows 走原路，其余平台用 Tk `winfo_screen*` |
| 标题栏染色 | `ctypes.windll.dwmapi` | `_apply_titlebar()` 开头 `if not win: return`，非 Windows 安全跳过 |
| 打开目录 | `os.startfile(d)`（Linux 会崩） | 新增 `open_folder()`：Windows `os.startfile` / macOS `open` / Linux `xdg-open` |
| 字体 | `FONT="Microsoft YaHei UI"`、`"Consolas"` | 新增 `pick_font()` + 候选表；`App.__init__` 里按系统实际字体定 `FONT` / `MONO` |

`pick_font()` 必须在建好 Tk root 之后调用（`font.families()` 依赖 default root），
所以放在 `App.__init__` 的 `super().__init__()` 之后、`_build()` 之前，用 `global` 回写模块级变量。

### 2.3 打包（`cx-download.spec` / `build.sh` / `make_icon.py`）

- `spec`：图标按平台选 —— Windows 用 `app.ico`，macOS 有 `app.icns` 就用、否则留空，
  Linux 传 `None`（ELF 没有图标位，传错平台的文件 PyInstaller 会直接报错）。
- 新增 `build.sh`：Linux / macOS 的打包 + 冻结自检一条龙，参数（`--no-network` /
  `--skip-selftest` / `--oids` / `--python`）与 `build.ps1` 对齐。
- `make_icon.py`：macOS 上额外产出 `app.icns`。
- 新增 `packaging/cx-downloader.desktop`：Linux 桌面入口模板。

### 2.4 CI（`.github/workflows/build.yml`，原 `build-exe.yml`）

- 单 job（`windows-latest`）→ 矩阵 `ubuntu-latest` / `windows-latest` / `macos-latest`，
  `fail-fast: false`。
- 打 tag 时三个平台各产一份，**挂到同一个 Release**，产物名带平台后缀：
  `cx-downloader-<tag>-{windows.exe,linux,macos}`。
- Linux job 先 `apt-get install -y tk`（PyInstaller 要把 Tk 运行库收进包）。
- 并行建 Release 有竞态：先 `gh release create`（失败忽略）再 `gh release upload --clobber`。

---

## 3. 改动前后关键结果对比

| 场景 | 改动前（v7） | 改动后（v8） |
| --- | --- | --- |
| Linux/macOS 自动读 Firefox 登录态 | 找不到 profile（写死 Windows 路径） | 能读到（本机实测：20 条 chaoxing cookie） |
| Linux 点「打开目录」 | `AttributeError` | 调 `xdg-open` |
| 非 Windows 屏幕尺寸 | 异常 → 窗口不居中/不夹屏 | `winfo_screen*` 正常 |
| 非 Windows 字体 | 回退默认 | 解析到 `Noto Sans CJK SC` / `DejaVu Sans Mono` |
| 非 Windows 打包 | 无 | `build.sh` / 三平台 CI |
| Windows 行为 | — | **保持不变**（走原分支） |

### 本机（Linux）验证记录

源码态（探针，见本次会话由 agent 执行的 `probe_gui.py`）：

```
[PASS] App 构造成功
[PASS] FONT=Noto Sans CJK SC   MONO=DejaVu Sans Mono
[PASS] screen_size = 1464x915
[PASS] 窗口几何 1120x805+172+20（居中且在屏内）
[PASS] _apply_titlebar 非 Windows 安全跳过
[PASS] open_dir -> ['xdg-open', ...]
```

- `python3 cx_download.py --help` 正常；`cx_download.py <公开预览objectid>` 端到端下载成功
  （`课程考核-成绩构成.pptx`，61.9KB，中文文件名落盘正常）。
- `selftest.py --no-network` → `SELFTEST PASS`。
- `./build.sh --no-network` → 产出 `dist/超星文件下载器`（15MB），
  **冻结自检 `frozen=True` PASS**，且在**干净环境**（`env -i`）下仍能独立运行
  （Tk 已收进包）；把打包产物拉起 GUI，窗口 `1120x805`「超星 / 学习通 文件批量下载器」正常显示。
- 注意：`probe_gui.py` / Tk 运行库是本机为验证临时准备的（`/tmp/opencode/`），
  不属于仓库产物。

---

## 4. 对下游交付物（论文/报告/接口）的口径影响

- **CLI / GUI 行为**：Windows 上完全不变；新增平台分支不改变原有逻辑与输出。
- **新增依赖**：无（`subprocess` 是标准库）。Linux 跑 GUI 需系统有 Tk（`python3-tk`）。
- **产物命名口径变化**：Release 附件从单个 `cx-downloader-<tag>.exe`
  变成 `cx-downloader-<tag>-windows.exe` / `-linux` / `-macos`。
  Windows 用户下载的文件名变了（内容等价）。
- **CI 文件重命名**：`build-exe.yml` → `build.yml`（workflow 名 `build-exe` → `build`）。

---

## 5. 回滚方式

把 `历史版本/登录态引导-v7-20260917/` 里的 `cx_download.py`、`cx_download_gui.py`、
`selftest.py` 覆盖回仓库根目录，并 `git checkout 9e9fcf0 -- .github/workflows/build-exe.yml`
（或把 `build-exe.yml` 恢复、删掉 `build.yml`）。`build.sh` / `packaging/` 可直接删。

---

## 6. 已知限制

- **CI 矩阵尚未实跑过**：Linux / macOS 两个 job 首次推上去要盯（尤其 Linux 的 Tk 运行库、
  macOS 的 Tk framework 收集）。本机只在 Linux 上真跑通了 `build.sh`。
- **macOS 只出单文件可执行程序，没打包成 `.app`**：能跑，但 Finder 双击体验一般
  （可能需右键「打开」绕过 Gatekeeper）。
- **Linux 免安装包依赖系统 Tk 运行库**（`libtk8.6` / `libtcl8.6`）；打包机上装了才会收进包。
  本机验证时 Tk 是用临时解包的 deb 提供的，普通桌面发行版自带。
- 自动读登录态仍**只认 Firefox**（三平台的 Firefox 都支持，含 snap），其它浏览器手贴 Cookie。
