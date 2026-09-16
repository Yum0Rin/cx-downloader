# 历史版本说明：GUI 从 tkinter 改为 CustomTkinter

- **改动名**：GUI改CustomTkinter-v1
- **日期**：2026-09-16
- **目录**：`历史版本/GUI改CustomTkinter-v1-20260916/`

## 1. 为什么改

初版图形界面用 Python 自带 `tkinter/ttk` 写，观感是 Windows 经典灰控件，与用户期望的
CustomTkinter（原生标题栏 + 圆角按钮 + 明暗双主题）风格不符。用户明确要求改用
`customtkinter`（已装 6.0.0，MIT，TomSchimansky/CustomTkinter）重做界面。

## 2. 改了哪些文件 / 核心差异

| 文件 | 说明 |
| --- | --- |
| `cx_download_gui.py` | **界面层整体重写**（tkinter/ttk → CustomTkinter）。`cx_download.py` 未改动。 |
| `历史版本/GUI改CustomTkinter-v1-20260916/cx_download_gui.py` | 改动前的 tkinter 版原样快照（本目录）。 |

核心差异：

- 控件：`ttk.Button/Entry/Label/Frame/Progressbar/Checkbutton/Radiobutton`
  → `CTkButton/CTkEntry/CTkLabel/CTkFrame/CTkProgressBar/CTkSwitch/CTkSegmentedButton`，
  文本框 `tk.Text` → `CTkTextbox`（自带圆角 + 细滚动条）。
- 模式选择：3 个 Radiobutton → 1 个 `CTkSegmentedButton`（仅原件 / 原件+PDF / 仅PDF）。
- 并发数：`ttk.Spinbox` → `CTkOptionMenu`（1/2/4/6/8/12/16）。
- 新增：标题栏区域 + 右上角外观切换（跟随系统 / 浅色 / 深色，`set_appearance_mode`）。
- 新增：卡片式分组（链接输入 / 保存目录 / 选项 / 日志）与统一配色常量。
- 保留：原生标题栏（CTk 默认即系统原生边框，未做无边框自绘）。
- 保留：设置持久化 `~/.cx_download_gui.json`、剪贴板粘贴、文件导入、打开目录、
  进度条 + 状态计数、日志分级着色、Ctrl+Enter 开始、关闭时确认。

## 3. 改动前后关键结果对比

下载功能（网络层）完全未动，因此**下载结果无差异**：

| 项 | 改动前（tkinter） | 改动后（CustomTkinter） |
| --- | --- | --- |
| 下载成功率 | 2/2 | 2/2（同一批 docx+pptx，原件+PDF 共 4 文件） |
| 文件字节数 | 与接口 `length` 一致 | 一致 |
| 并发/重试/Referer 逻辑 | 同 | 同（复用 `cx_download.process_one`） |
| 线程模型 | 主线程读 Tk 变量 → worker 下载 | 同（避免 `main thread is not in main loop`） |

唯一差异是**界面观感与交互控件**，不涉及口径变化。

## 4. 对下游交付物的口径影响

- 桌面快捷方式 `超星文件下载器.lnk` 指向的脚本路径未变，**无需重建快捷方式**。
- 命令行版 `cx_download.py` 未改动，原有命令行用法与输出格式不变。
- 新增依赖：`customtkinter`（已在用户环境安装 6.0.0）。命令行版仍零第三方依赖。

## 5. 回退方式

若需回到 tkinter 版，直接用本目录的 `cx_download_gui.py` 覆盖上一级同名文件即可。

---

# 追加修订 v2：浅色配色重审 + 标题栏着色 + 日志主题化（2026-09-16）

## 6. 为什么改

v1 的 CustomTkinter 版初稿配色被用户判定「好丑」。逐项复核后确认是 5 个具体缺陷：

1. **系统标题栏是青色**（Windows 强调色 `#0F8A8A`），与朱砂/黛蓝主色严重冲突——最刺眼的一处。
2. 输入框 `#F7F8FA` 放在纯白卡片上**几乎看不出边界**。
3. 次要按钮做成「透明底 + 描边」的幽灵按钮，观感像禁用态。
4. 次要文字 `#7A8794` 在浅底上对比度偏低。
5. 进度条 0 值时左端残留一个红点（`CTkProgressBar` 在 value=0 仍绘制填充圆角）。
6. 日志框写死纯黑 `#151A21`，**浅色主题下突兀**（用户明确点名："浅色的日志框不对，全黑"）。

## 7. 改了哪些文件 / 核心差异

仅改 `cx_download_gui.py`，`cx_download.py` 仍一字未动。

| 项 | v1 初稿 | v2 |
| --- | --- | --- |
| 配色组织 | 散落的 `C_XXX = (浅, 深)` 常量 | `LIGHT` / `DARK` 两个字典 + `pair(key)` 取元组 |
| 标题栏 | 跟随系统强调色（青） | `DwmSetWindowAttribute` 染成黛蓝/深灰，文字色同步 |
| 模式/外观选择器 | `CTkSegmentedButton` | 自研 `ToggleGroup`（见下） |
| 次要按钮 | 透明底 + 描边 | 实心浅灰底 `#EDF1F6`，悬停加深 |
| 日志框 | 写死深色 | `LOG_STYLE` 按主题切换底色/文字/tag 色 |
| 进度条 0 值 | 残留红点 | value=0 时把填充色设为轨道色 |
| 窗口尺寸 | 写死 `920x740` | `_fit_geometry()` 按物理分辨率 ÷ 缩放换算，保证放得下 |

**为什么要自研 `ToggleGroup`**：`CTkSegmentedButton` 只有**一个** `text_color`，选中态一填
饱和色（朱砂/黛蓝）文字就和底色撞成同色而隐形（实测「浅色」二字直接看不见）；而且它
会在段间自己画分隔线。改用一排 `CTkButton` 后，选中态可以「饱和底 + 白字」、未选中态
「浅灰底 + 深字」，两全其美。

## 8. 顺带修掉的 3 个真 bug

1. **保存的外观设置在启动时不生效**：只把值读进 `StringVar`，从没调过
   `set_appearance_mode()`，导致选「浅色」重启后仍是系统主题。
2. **外观按钮选中态与真实主题脱节**：`ToggleGroup` 只在构造时读一次变量；
   已把两个 toggle 组存为 `self.tg_appearance` / `self.tg_mode`，并在主题切换时
   一并 `_restyle()`。
3. **浅色日志框全黑**（用户直接反馈）。

## 9. 改动前后关键结果对比

下载功能（网络层）依然未动，**下载结果无差异**：同一批 docx+pptx，原件+PDF 共 4 个文件
全部成功，字节数与接口 `length` 一致（320275 / 313360 / 86759 / 63352）。
差异只在界面观感与交互。

## 10. 快照纪律说明（如实记录）

本轮**未能在改动前**对「v1 的 CustomTkinter 初稿」单独存档——它是 tkinter 版改成 CTk 后
约 20 分钟内就被推翻重做的中间态，且从未作为正式版交付过。本目录中保留的
`cx_download_gui.py` 是**最早的 tkinter 版**，仍是有效的回退点。
自 v2 起，后续任何推倒重来式改动都会先按要求快照。

## 11. 对下游交付物的口径影响

- 桌面快捷方式 `超星文件下载器.lnk` 指向路径未变，**无需重建**。
- 命令行版 `cx_download.py` 用法与输出不变。
- 依赖仍只有 `customtkinter`（6.0.0）；标题栏着色走 `ctypes` 调 `dwmapi`，无新依赖。
- 已知环境约束：该机为 **175% 缩放 + 2560×1600 物理屏**，`_fit_geometry()` 已按此换算；
  换到其它缩放比例的机器上会自动重算，无需改代码。

