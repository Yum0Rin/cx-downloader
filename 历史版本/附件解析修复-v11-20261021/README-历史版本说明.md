# 历史版本：附件解析修复-v11-20261021

> **本目录内容 = 改动前（修附件解析、加「换号」之前）的那一版。**
> 当前正式版是仓库根目录里改动后的版本；本目录仅存档、不参与交付。

## 1. 为什么改

用户报「课程里明明一堆文档，扫出来只有 16 个」。定位到学习通改版：

同一门课 42 个章节，附件有三种 HTML 写法，旧代码只认第一种。

| 写法 | 场景 | 旧解析 |
| --- | --- | --- |
| `<iframe class="ans-attach-online" data="{...}">` | 老模板 | 命中 |
| `<div class="ans-attach-ct"><iframe class="downloadfile" data="{...}">` | 新模板，多数课件 | 漏 |
| `<iframe class="ans-module ans-insertdoc-retract" module="insertdoc" data="{...}">` | 编辑器插入的文档 | 漏 |

逐章统计：老写法 16 个、新写法 41 个，按 objectid 去重合计 **99 个**，与课程门户
「教学资源」列表条目数一致。漏掉的几乎全是文档类（pdf/docx/doc/pptx/ipynb/csv/xlsx/py/npy），
视频章因为还走老模板反而没漏 —— 所以现象是「文档全没了、视频还在」。

顺带处理账号切换不透明：手动 Cookie 优先于 Firefox 登录态，浏览器换号后旧 Cookie 仍在，
工具会一直用旧号扫，界面上却看不出是哪个号。

## 2. 改了哪些文件 / 核心差异

- `cx_download.py`
  - `fetch_chapter_attachments()`：**不再按 class 名抓标签**，改成扫所有
    `data="{...objectid...}"` 再解析 JSON，三种写法一次覆盖；按 objectid 去重
    （`ans-attach-ct` 外层容器和内层 iframe 可能各带一份相同 data）。
  - 新增 `fetch_account_name()`：读 `passport2.chaoxing.com/mooc/accountManage` 的
    `#messageName`，失败返回空串（不影响扫描）。
  - `process_one()`：「仅 PDF」模式下没有 `pdf` 字段的格式（csv/py/ipynb/npy/txt/exe）
    改标「跳过」，不再算失败；PDF 直链 404 时给「云盘没生成转码，换原件模式」的提示。
- `cx_download_gui.py`
  - 登录态那行追加「· 账号 某某」，启动时与扫描前各刷一次（后台线程，失败静默）；
  - 新增「换号」按钮：清空手动 Cookie → 重读 Firefox `cookies.sqlite` → 重新取账号名；
  - 登录失效（`need_login`）时清掉账号名，避免显示过期信息。
- `CHANGELOG.md`：新增 `[1.2.0]` 段落。
- `docs/问题记录/2026-10-21-章节附件漏抓.md`：本次问题的留痕。

## 3. 改动前后关键结果对比

| 场景 | 改动前 | 改动后 |
| --- | --- | --- |
| 同一门课（42 章）扫描 | 16 个文件 | 99 个文件 |
| 文档类（pdf/docx/pptx/ipynb/csv/xlsx） | 几乎全漏 | 全部列出 |
| 视频章（老模板） | 15 个 | 15 个（不变，未重复计入） |
| 界面上能否看出当前账号 | 只显示「手动 Cookie（N 字符）」 | 显示账号姓名 |

## 4. 对下游交付物的口径影响

- 扫描结果数量会明显变大：以前「这门课没资料」的判断可能是漏抓，重扫后需重新核对。
- 同一份附件在三种写法里只计一次（按 objectid 去重），任务列表不会出现重复行。
- 「资料」页（`coursedata/stu-datalist`）逻辑未动；「暂无文件」仍是真空。
- 账号名来自额外的一次页面请求，**不影响**扫描成功与否；断网/接口改版时该行只是不显示姓名。

## 怎么回滚

用本目录里的 `cx_download.py`、`cx_download_gui.py`、`CHANGELOG.md` 覆盖仓库根目录同名文件，
删掉 `docs/问题记录/2026-10-21-章节附件漏抓.md`，再按项目流程重打包。
（本快照对应 v1.1.2 之后的提交状态，即本次改动之前。）
