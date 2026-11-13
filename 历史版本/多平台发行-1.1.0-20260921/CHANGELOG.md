# 更新日志

本项目所有值得记的变更都写在这里。版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.1.0] - 2026-09-21

多平台发行：在原有 Windows 基础上适配 macOS / Linux，并让三平台都能出免安装包。

### 新增

- **macOS / Linux 支持**
  - 自动读取本机 Firefox 登录态时，按平台定位 `cookies.sqlite`：
    Windows `%APPDATA%\Mozilla\Firefox`、macOS `~/Library/Application Support/Firefox`、
    Linux `~/.mozilla/firefox` 及 snap 版 `~/snap/firefox/common/.mozilla/firefox`。
  - 界面字体按系统实际安装的挑（不再写死微软雅黑 / Consolas），
    Linux/macOS 上中文不再糊。
  - 「打开目录」跨平台：Windows `os.startfile` / macOS `open` / Linux `xdg-open`。
  - 屏幕尺寸与标题栏着色按平台分支，非 Windows 安全跳过（不再裸调 `ctypes.windll`）。
- **Linux / macOS 打包脚本 `build.sh`**，参数（`--no-network` / `--skip-selftest` /
  `--oids` / `--python`）与 `build.ps1` 对齐。
- **CI 三平台矩阵**：`ubuntu-latest` / `windows-latest` / `macos-latest`；
  打 tag 时三平台产物挂到**同一个 Release**。
- Linux 桌面入口模板 `packaging/cx-downloader.desktop`。
- `make_icon.py` 在 macOS 上额外生成 `app.icns`。

### 变更

- Release 附件命名从 `cx-downloader-<tag>.exe` 改为带平台后缀：
  `cx-downloader-<tag>-windows.exe` / `-linux` / `-macos`。
- CI 工作流文件由 `build-exe.yml` 改名为 `build.yml`（workflow 名 `build-exe` → `build`）。

### 修复

- 仓库 `core.filemode=false`（Windows 挂载盘）导致 `build.sh` 在 git 里没有可执行位，
  CI 里直接 `./build.sh` 报 `Permission denied` —— 改用 `bash build.sh` 调用。

## [1.0.0] - 2026-09-17

首个正式版本。

- 粘贴课程页链接 → 扫描**章节任务点** + **「资料」页**（递归文件夹）→ 勾选 → 批量下载。
- CustomTkinter 图形界面，另有**纯标准库**的命令行版 `cx_download.py`（可单独当 CLI 用）。
- 自动读本机 Firefox 登录态，或在界面 / `--cookie` 手贴 Cookie。
- 本地已有且**内容相同**的文件自动跳过（字节数 + 前 64KB md5，靠 HTTP Range 只取 64KB）。
- 可只下原件 / 只下 PDF / 两者都下；落点目录层级四级可选。
- 一键打包 Windows exe（PyInstaller，自带冻结自检 `--selftest`），GitHub Actions 自动发版。

[1.1.0]: https://github.com/Yum0Rin/cx-downloader/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/Yum0Rin/cx-downloader/releases/tag/v1.0.0
