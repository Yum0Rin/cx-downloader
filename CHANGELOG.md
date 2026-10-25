# 更新日志

本项目所有值得记的变更都写在这里。版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.3.3] - 2026-10-25

### 新增

- **交流群**：设置页加了一行「想联系作者、反馈问题或交流使用，可以加 QQ 群 1129676506」，
  旁边按钮一键复制群号。

## [1.3.2] - 2026-10-25

### 新增

- **设置页「支持作者」**：低调放在 GitHub / Release 按钮旁边，点开并排显示
  微信 / 支付宝收款码；不自动弹窗、不打扰使用。收款码图片放在 `assets/`，打包时一起带上。

## [1.3.1] - 2026-10-25

### 修复

- **下载进度条只在下载时出现**：空闲时不再在操作栏里留一条看不懂的空线。
- **状态文字位置**：「共 N 个 / 就绪」不再贴着窗口最右边，和按钮区留出间距。
- 设置 / 账号选择 / 更新弹窗的高度按内容自适应，去掉底部大片空白。

### 文档

- 更新主界面截图：示例课程、示例目录、示例账号，已去隐私化。
- README 补充右上角「设置」能做什么。

## [1.3.0] - 2026-10-25

### 新增

- **设置页 + 主题色**：右上角「设置」里可选预设色板或打开系统色盘自定义主题色，
  外观（跟随系统 / 浅色 / 深色）、扫描缓存、检查更新、GitHub / Release 入口也收进来了。
  默认主题色改为黛蓝，不再是原来的朱砂红。
- **扫描缓存 + 并发提速**：同一门课 10 分钟内重复扫描直接读本地缓存（秒开）；
  章节卡片请求改成并发。实测 24 章从 33 秒降到 10.5 秒。再点一次「扫描」会跳过缓存强制刷新。
- **失败一键重试 + 队列持久化**：下载失败的项会留一个「重试失败 (N)」按钮；
  任务列表（含勾选状态）自动保存到本地，关掉程序再开会恢复，不用重新扫描。

### 变更

- 设置项保存改为合并现有配置，不再整个覆盖（避免冲掉更新提醒等字段）。
- 下载时也会用「Firefox 实时」Cookie（之前只有扫描用，下载会退回磁盘 Cookie）。
- 主题色保存后需要重启程序才完全生效（设置里有「保存并重启」）。
- 设置 / 账号选择 / 更新弹窗按内容自适应高度，不再留一大片空白。

## [1.2.3] - 2026-10-24

### 新增

- **Firefox 实时登录态**：学习通的登录票据（`_uid` / `p_auth_token` / `vc3` 等）
  经常只在浏览器内存里，磁盘 `cookies.sqlite` 里没有。现在会通过 Firefox 自带的
  Marionette 协议读**运行中浏览器**的 Cookie，启动后自动显示当前登录账号；
  「换号」里也能列出实时账号（多个窗口 / 容器可对应多个账号）。
  Firefox 不是以 `--marionette` 启动时，弹窗里有「让 Firefox 开启实时读取」按钮，
  会重启一次浏览器（标签页会自动恢复）。
- **更新提醒**：启动后查 GitHub 最新 Release，有新版本时右上角出现「更新 vX.Y.Z」
  按钮并弹窗展示该版本的更新说明（可「忽略此版本」，以后不再弹）。
- **GitHub 入口**：右上角 GitHub 按钮，直接打开项目主页。

## [1.2.2] - 2026-10-24

### 新增

- **多账号识别与选择**：点「换号」会读取本机 Firefox 里所有能读到的学习通账号
  （不同 profile、或 Multi-Account Containers 的容器各算一个），列出姓名和来源，
  选哪个就切到哪个，并立即保存。读不到完整登录态时也会给出手动粘贴入口。
  一个 Firefox 配置通常只保留一个账号的登录态，想同时存多个号需要多个 profile / 容器；
  浏览器刚登录时 Cookie 可能还没写盘，刷新一下学习通页面即可。

### 变更

- 「换号」不再是简单的「清空手动 Cookie 回退 Firefox」，改成账号选择弹窗；
  里面仍保留「用 Firefox 自动」和「手动粘贴 Cookie」。

### 文档

- README 主界面截图里的账号姓名已隐去。

## [1.2.1] - 2026-10-24

### 修复

- **同一章里后面的卡片整张漏扫（习题集扫不出来）**：学习通把一章拆成多张「卡片」
  （PPT / 习题集 / 案例 / 课程思政…），每张卡用独立的 `num` 索引（0 起）。
  旧代码只请求 `num=0`，所以每章只拿到第一张卡的附件 —— 实测某门课的习题集
  扫不出来、PPT 后面的视频和文档也一起漏掉。现在逐张请求到越界为止
  （越界页 `<title>` 为空、没有附件，连空两张即停），并给每个附件带上卡片名。
  实测同一门课：修复前 24 个文件，修复后 108 个（含 17 份习题集 PDF）。

## [1.2.0] - 2026-10-21

### 修复

- **章节附件大面积漏抓（只认出 16/99）**：学习通改版后，同一份附件有三种 HTML 写法 ——
  老的 `class="ans-attach-online"`、新的 `class="ans-attach-ct"` 里套 `downloadfile` iframe、
  以及编辑器里插进正文的 `ans-insertdoc-retract`。旧代码按 class 名抓标签，只认第一种，
  于是文档类（pdf/docx/pptx/ipynb/csv/xlsx）几乎全丢，视频章因为还用老模板反而没丢 ——
  表现为「文档全没了、视频还在」，很容易被当成老师没传。
  现在改成扫所有带 `data="{...objectid...}"` 的标签再解析 JSON，三种写法一次覆盖，
  按 objectid 去重。实测同一门课 42 章：修复前 16 个，修复后 99 个，与课程门户
  「教学资源」条目数一致。详见 `docs/问题记录/2026-10-21-章节附件漏抓.md`。
- **「仅 PDF」模式下没有 PDF 版的文件不再记「失败」**：csv / py / ipynb / npy / txt / exe
  这类格式服务端不转码，`/ananas/status/` 只给 `download`、没有 `pdf`，模式选「仅 PDF」
  就无事可做。以前报「接口未返回可下载地址」并计入失败，看着像文件坏了；现在标
  「跳过：该格式没有 PDF 版（可在选项里开『无 PDF 版时下原件』）」。
- **PDF 直链 404 给具体原因**：云盘上没生成/没保留这份转码时，报
  「云盘上没有这份 PDF 转码（404）—— 换『原件+PDF』或『仅原件』下原件」，
  不再只甩一句 `HTTP Error 404`。
- **重扫全标「新文件」、自动跳过失效**：查重时按**原名**在**课程目录**里找，但实际落盘是
  「仅 PDF」的 `<stem>.pdf`、而且在「分组 / 章节」子目录里，于是明明下过也全算新文件。
  现在两种候选名（原件名 / `.pdf`）都看，落点目录按当前层级勾选算；命中的是 PDF 转码时
  拿 `pdf` 地址比内容（拿原件比必然不等）。下载模式也进 `scan_key`，换模式会重新判定。

### 新增

- **「仅 PDF」模式下的原件兜底开关**：选项里新增「仅 PDF 时，无 PDF 版的格式也下原件」
  （默认开，CLI 对应 `--pdf-fallback`）。csv / py / ipynb / npy / xlsx 这类服务端不转码的
  格式，开了就下原件、能转 PDF 的照旧只给 PDF；关掉则维持「跳过」并说明原因。
  配置项存在 `~/.cx_download_gui.json` 的 `pdf_fallback`。
- **登录态显示当前账号姓名**：手动 Cookie 优先于 Firefox 登录态，换了账号却看不出来时，
  很容易把「扫到的不是这门课」误判成课程本身没资料。现在登录态那行会带上「· 账号 某某」
  （后台取，取不到就留空，不影响扫描）。
- **「换号」按钮**：一键清空手动 Cookie、重读 Firefox 的 `cookies.sqlite`、重新确认账号名。
  浏览器里换号后点它即可，不用再手删 Cookie。
- **视频也下载**（选项开关，默认关；CLI `--video`）：视频任务点没有 PDF，但
  `/ananas/status/` 里有 `http`（学习通转好的 mp4）和 `download`（原件）。
  - **转码 mp4**（默认）：服务端压过的版本，几十 MB，普通播放器直接放；画质是被压过的。
    实测同一门课原件 809.7MB → 转码 33.8MB（约 1/24）。
  - **原件**：老师上传的那份（`.mov`/`.mp4`），画质码率最好，但几百 MB 到 1GB，
    `.mov` 可能还要装解码器。
  - 没有转码版的视频自动退回原件；关着则维持「跳过」。CLI 用 `--video-original` 取原件。

## [1.1.2] - 2026-10-09

### 变更

- **视频任务点改为「跳过」而不是「失败」**：mp4 等视频任务点的 `/ananas/status/` 只返回
  `mp3/screenshot/dtoken/duration`，没有 `download/pdf`，工具结构上无法下载。现在识别为视频并
  标记「跳过（视频，不支持）」，不再计入失败。汇总相应改为「成功 X / 跳过 Y / 失败 Z」
  （CLI 与 GUI 一致）。

### 修复

- **下载支持断点续传**：`download()` 中途出错时**保留 `.tmp`**，重试带 `Range` 从断点
  接着下，不再从 0 重来。此前遇到云盘「冷节点」（尤其服务端转好的 PDF 所在的
  `s3.cldisk.com`：首次访问只吐一点数据就长时间停滞）会反复「下到卡点 → 读超时 →
  清空重下 → 再卡」，永远下不完，界面上表现为「卡住」，最终报
  `The read operation timed out`。现在首个卡点之后缓存变热，续传即可秒完。
  服务端忽略 `Range`（照旧回 200）时自动回退整包重下；`416`（起点已到末尾）按下载完成处理。

## [1.1.1] - 2026-10-09

### 修复

- **原件本身就是 PDF 的文件被误报失败**：超星 `/ananas/status/` 返回的
  `status` 是「转码状态」，不是「能不能下」。已转码 / 无需转码（原生 PDF）的文件
  `status` 可能是 `failed`，且没有 `pdf` 字段、只有 `download`。旧逻辑在
  `status != "success"` 时直接判失败（表现：报 `接口返回 status='failed'`、
  列表里只有一长串 objectid）。现在改成**按实际可用地址判断**：有 `download`/`pdf`
  就下；「仅 PDF」模式下若原件已是 PDF，则把 `download` 直接落成 `.pdf`。
- 失败项现在显示**文件名**而非裸 objectid（`res["filename"]` 在取名后即写入，
  不再只在成功路径设置），日志更好认。

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

[1.3.3]: https://github.com/Yum0Rin/cx-downloader/compare/v1.3.2...v1.3.3
[1.3.2]: https://github.com/Yum0Rin/cx-downloader/compare/v1.3.1...v1.3.2
[1.3.1]: https://github.com/Yum0Rin/cx-downloader/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/Yum0Rin/cx-downloader/compare/v1.2.3...v1.3.0
[1.2.3]: https://github.com/Yum0Rin/cx-downloader/compare/v1.2.2...v1.2.3
[1.2.2]: https://github.com/Yum0Rin/cx-downloader/compare/v1.2.1...v1.2.2
[1.2.1]: https://github.com/Yum0Rin/cx-downloader/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/Yum0Rin/cx-downloader/compare/v1.1.2...v1.2.0
[1.1.2]: https://github.com/Yum0Rin/cx-downloader/compare/v1.1.1...v1.1.2
[1.1.1]: https://github.com/Yum0Rin/cx-downloader/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/Yum0Rin/cx-downloader/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/Yum0Rin/cx-downloader/releases/tag/v1.0.0
