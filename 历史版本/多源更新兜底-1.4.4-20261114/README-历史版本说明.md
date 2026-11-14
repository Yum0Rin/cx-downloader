# 快照：多源更新兜底，改动前 v1.4.4

- 快照时间：2026-11-14
- 基线 commit：`8759343`（工作区干净，仅多了本快照目录）
- 快照内容：`cx_download.py`、`cx_download_gui.py`、`selftest.py`、`README.md`、`CHANGELOG.md`，哈希见 `文件哈希.txt`

## 为什么改

本机 `fastgithub`（PID 6924，WinDivert 驱动）把 `github.com` / `api.github.com` /
`objects.githubusercontent.com` 的 DNS 全部劫持到 `127.0.0.1`，转发时好时坏：
早前实测直连 GitHub IP 超时 / 被 RST、资产 CDN 30s 超时，更新检查要等约 25.8 秒
才走完兜底，下载 exe 必失败。**这是本机劫持与链路不稳定，不是学校网络把境外掐了**
（后来复核时 Release API 其实能 200，见下方「复核」）。现有「一键更新」只有
`api.github.com` → `github.com/.../releases/latest` 两条路径，串行各重试 2 次 ×12 秒，
任一环卡住就整体拖死。

**硬性约束：校园网万万不可开代理。** 因此这次不引入、不建议、不依赖任何代理
（Clash / TUN / 系统代理 / FastGithub 都不碰），兜底只能走「境内直连可达源」和「离线导入」。

## 改了什么（已回填）

| 层 | 改动前 | 改动后 |
| --- | --- | --- |
| 更新检查 | GitHub API → HTML 302 串行，2×12s | 多源并行竞速：境内清单 URL（可配置）+ GitHub API + HTML，短超时，谁先回用谁 |
| 版本信息 | 只认 GitHub Release JSON | 增加 `latest.json` 境内清单协议，统一解析成同一份 info |
| 下载 | 只认 1 个 `asset_url`，socket 超时 120s | `asset_urls` 多源逐个回退，短连接超时，换源前清残包 |
| 完全断网境外 | 只能手动开浏览器碰运气 | 设置里「从本地文件更新」，选 exe 校验后走同一套替换流程 |
| 设置界面 | 引导填本地代理（如 Clash 127.0.0.1:7897） | 改为引导境内更新源 / 离线导入，代理输入框保留但不再作为推荐方案 |
| 自检 | 无更新通道单测 | 增加不联网的清单解析、源优先级、离线包校验单测 |

## 前后对比（实测，均为合成样例 / 本机 127.0.0.1 服务，不涉及代理）

| 项 | 改动前 | 改动后 |
| --- | --- | --- |
| 更新检查（部分域名挂） | 串行 2×12s + HTML 2×10s，早前实测约 25.8s 才走完 | 多源并行，实测 3.0~4.8s 返回 None（总预算 10s） |
| 有境内清单时 | 无此路径 | 实测 0.21~0.22s 返回 `v9.9.9 / 境内更新清单` |
| 下载地址 | 1 个，挂了就失败 | 2 个源实测：源 1 返回 404 → 自动换源 2，下载 1048588 B 并通过 sha256 校验 |
| `python -m py_compile`（3 个 .py） | — | 通过 |
| `python selftest.py --no-network` | 5 项 PASS | 12 项 PASS（新增 7 项更新兜底用例） |

验证脚本：`%TEMP%\cx_multisrc_verify.py`（合成数据，非真实 Release；服务只监听 127.0.0.1）。

发版前源码哈希（与 `文件哈希.txt` 的改动前哈希逐项不同，确认跑的是改后版本；
`cx_download.py` / `CHANGELOG.md` 之后又因「升版本到 1.4.5」变过一次）：

```
cx_download.py      68E4CFEE6D9416107E85C096BBCEED445853E2395DB0AA4358A6A37DCFF91983
cx_download_gui.py   D1EA05F4C62F8EBB3A5547A6C809E24A200B42CC5199E45193824F8F1B5FEC23
selftest.py          E26B8F0CBD41F5E053286FB685F6653AF96C675279DA5FCCF4AC2259C12AC2B4
README.md            3D5F9223EA9DF9169F32BB0002E1E24C4BB374CB137A9BB8DD2CCCC116A4CD44
CHANGELOG.md         9A9248BA762F337FB262DF151E07868A9DDEB850AF8CB4E5163141F421A0B7E1
```

## 复核（用户反馈「我现在能打开 GitHub 啊」之后重测）

同一台机器、同一个 SSID `S415`、DNS 仍全部解析到 `127.0.0.1`、`fastgithub` 仍在跑：

| 目标 | 结果 |
| --- | --- |
| `api.github.com/repos/.../releases/latest` | **200，1.41s**，真实 Release `v1.4.2`、3 个资产、带 sha256 |
| 强制 `current=0.0.1` 走 API 源 | 0.54s 解析出完整 info（**真实 GitHub 联网验证已补做**） |
| `github.com` 根路径 / `/releases/latest` 302 | 8s 读超时 |
| `objects.githubusercontent.com` 资产（Range 前 1KB） | 8s 读超时 |
| `check_update()` 默认调用 | 返回 None —— 原因是**本地 `__version__=1.4.4` 比线上 v1.4.2 新**，不是连不上 |

结论修正：不是「校园网掐境外」，而是 FastGithub 劫持导致**部分域名/部分路径**卡死；
浏览器能开网页 ≠ 更新链路能下 exe。多源竞速正好覆盖这种「某个源挂、另一个源通」的状态，
兜底改动依然有必要，但动机描述以本节为准。

**发版**：用户授权后发布为 **v1.4.5**（不复用 1.4.4，因为桌面 / `dist` 里那份 1.4.4 是
改动前的旧包，同名会让旧包 `check_update()` 认为「已是最新」而永远收不到更新）。
commit `2f0f64d` → tag `v1.4.5` → CI run `43567839591`，三平台 job 全部成功
（47s / 57s / 47s），Release 三个资产齐全且带 sha256，正文取 CHANGELOG 的 `[1.4.5]` 段。

推送时第一次 `git push` 报 `HTTP 500 / unexpected disconnect`（git 走 `github.com`，
而该域名被本机 FastGithub 劫持转发）；改用 `http.version=HTTP/1.1` +
`http.postBuffer=524288000` 重试一次即成功，**全程未开代理**。

真实链路验证（非合成）：以旧版 `1.4.4` 身份调 `check_update(current="1.4.4")`，
从 GitHub API 拿到 `v1.4.5` / 资产 URL / size 15169124 / sha256，与 Release 一致。

**没做/留给后续**：`latest.json` 的生成与同步到境内静态托管（需用户提供托管地址）；
资产 CDN 全量下载验证（当前该域名仍超时，不反复硬试，
按「从本地文件更新」或境内源绕过）。

## 怎么回滚

- 最省事：把本目录里 5 个文件复制回仓库根覆盖（先确认没有别的改动要保留）。
- 或 `git -C <仓库> checkout -- cx_download.py cx_download_gui.py selftest.py README.md CHANGELOG.md`
  （本快照基于 `8759343`，回退到该提交即可）。
- 回滚后 `~/.cx_download_gui.json` 里新增的 `update_mirrors` 字段会被忽略，不用清理。
