# 历史版本：状态误报修复-v9-20261009

> **本目录内容 = 改动前（修复前）的那一版。**
> 当前正式版是仓库根目录里改动后的版本；本目录仅存档、不参与交付。

## 1. 为什么改

用户下载「261学期 软件项目管理原理与实践」时，第 19 章「人工智能东山再起」那个附件报错：

```
✘ f36d062fa2e2a7e3110fabfda427cb27
    接口返回 status='failed'
```

实测该 objectid 的 `/ananas/status/` 返回：

```json
{"filename":"人工智能东山再起.pdf","length":130431,
 "download":"http://d0.cldisk.com/download/f36d...&ak_=...",
 "status":"failed"}          // 无 pdf 字段
```

`download` 直链实测**能完整下到 130431 字节，文件头 `%PDF`**。

根因：超星的 `status` 是**转码状态**，不是「能不能下」。
- 原件本身已是 PDF → 服务端无需转码 → 没有 `pdf` 字段；
- 转码状态此时为 `failed`。

而旧逻辑（`cx_download.py` 的 `process_one`）在 `status != "success"` 时**直接判死**，
还没看有没有可用地址，于是把可正常下载的文件误报成失败。

## 2. 改了哪些文件 / 核心差异

- `cx_download.py`
  - `process_one()`：删掉「`status` 必须为 `success`」的硬门槛，改成
    **按实际可用地址（`download` / `pdf`）决定能不能下**；
    「仅 PDF」模式下若 `pdf` 缺失但原件扩展名就是 `.pdf`，则把 `download` 落成 `.pdf`。
  - `res["filename"]` 提前到取名之后即写入（原来只在成功路径写），失败时也能报文件名。
  - CLI 失败行：`[失败] {objectid}` → `[失败] {filename or objectid}`。
- `cx_download_gui.py`
  - `_show()` 失败行：`✘ {objectid}` → `✘ {filename or objectid}`。
- `CHANGELOG.md`：新增 `[Unreleased] → 修复` 条目。
- `selftest.py`：**未改动**（仅为存档完整一并快照）。

## 3. 改动前后关键结果对比

| 场景 | 改动前 | 改动后 |
| --- | --- | --- |
| 练习 oid，仅 PDF 模式 | 报错 `接口返回 status='failed'`，0 字节 | OK，落 `人工智能东山再起.pdf`，130431 B，`%PDF` |
| 同 oid，原件+PDF 模式 | 报错 | OK，原件 + PDF 都落盘 |
| 同 oid 重跑（不覆盖） | —— | `已存在，跳过` |
| 失败日志显示名 | 一长串 objectid | 文件名（取不到才回退 objectid） |

## 4. 对下游交付物的口径影响

- 之前把这类文件记成「失败」的统计会变化：重新扫描下载会由 失败 转为 成功/跳过，属预期。
- 界面对「已存在 / 内容相同」的预判逻辑未变（仍按扩展名预判落点）。
- 未发版；本机 exe 已用改动后源码重打并替换桌面 `大学の残骸\超星文件下载器.exe`。

## 怎么回滚

用本目录里的 `cx_download.py` / `cx_download_gui.py` 覆盖仓库根目录同名文件，
再按项目流程重打包即可。
