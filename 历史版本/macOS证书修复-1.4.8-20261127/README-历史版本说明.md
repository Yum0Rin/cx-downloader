# 历史版本：macOS 证书修复 + Windows 圆角修复-v1.4.8-20261127

> **本目录内容 = 改动前（1.4.7 正式版）的 `cx_download.py` / `cx_download_gui.py`。**
> 当前正式版是仓库根目录里改动后的版本；本目录仅存档、不参与交付。

## 1. 为什么改

两件用户可见的问题：

1. **macOS 扫描报 `CERTIFICATE_VERIFY_FAILED` / `self-signed certificate in certificate chain`**
   打包版 PyInstaller 自带一套 CA，不认系统钥匙串里装的根证书（校园网 / 企业代理 / 自签
   证书场景）。用户临时用 `SSL_CERT_FILE=/etc/ssl/cert.pem` 启动能通，说明就是证书库问题。
2. **Windows 界面圆角处有近黑色直角块**（macOS 正常）。根因是 CustomTkinter 6.0.0 在
   Windows/Linux 默认用 `font_shapes`（拿自定义 shapes 字体画抗锯齿圆角），该方式在
   Windows 上渲染有回归，圆角被近黑直角块切出缺口；macOS 走 `polygon_shapes` 不受影响。

顺带把界面上有歧义的「缺失」统一改成「未下载」（文件没丢，只是本地还没有）。

## 2. 改了哪些文件 / 核心差异

- `cx_download.py`
  - 新增 `import ssl`，以及统一的网络入口 `http_open(req, timeout)`：
    macOS 上 `ssl.create_default_context()` + `load_verify_locations("/etc/ssl/cert.pem")`，
    其余平台用默认 opener。**仍是严格校验，不关验证**；证书库异常时退回默认，不让程序起不来。
  - 全文件把 `urllib.request.urlopen(req, timeout)` 换成 `http_open(req, timeout)`。
  - 用户可见文案「缺失」→「未下载」。
  - `__version__` 1.4.7 → 1.4.8。
- `cx_download_gui.py`
  - 顶部新增 `_use_robust_corner_rendering()`：把 CTk 的 `DrawEngine.preferred_drawing_method`
    从 `font_shapes` 切到 `polygon_shapes`（三平台一致，规避 Windows 圆角黑块）；
    取不到内部属性就跳过，不影响旧版 CTk 启动。
  - 更新包下载不再自建 opener，改用 `core.http_open()`。
  - 「缺失」→「未下载」（状态标签、统计、日志）。
- `README.md`：macOS 完整安装步骤重写（逐条命令、Gatekeeper 放行、常见报错对照）。
- `CHANGELOG.md`：新增 1.4.8 段。

## 3. 前后对比

| 场景 | 1.4.7（本目录存档） | 1.4.8 |
| --- | --- | --- |
| macOS 扫描（代理/自签证书） | `CERTIFICATE_VERIFY_FAILED`，扫描失败 | 正常；不用再手动设环境变量 |
| Windows 圆角 | 按钮/输入框/卡片四角近黑直角块 | 平滑圆角，与 macOS 一致 |
| 状态文案 | 「缺失」有歧义 | 「未下载」 |

## 4. 怎么回滚

把本目录的 `cx_download.py.before` / `cx_download_gui.py.before` 覆盖回仓库根目录的
`cx_download.py` / `cx_download_gui.py`，重新 `build.ps1` 打包即可（README / CHANGELOG
可一并回退，不影响功能）。macOS 老版本可临时用
`SSL_CERT_FILE=/etc/ssl/cert.pem ./cx-downloader-<版本>-macos` 启动绕过证书问题。
