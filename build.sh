#!/usr/bin/env bash
#
# 把「超星文件下载器」打包成单文件可执行程序（Linux / macOS），
# 并用出货的那个二进制自身跑冻结自检。
#
# 用法：
#   ./build.sh                      # 打包 + 自检（含联网）
#   ./build.sh --no-network         # 自检只验打包完整性，不联网
#   ./build.sh --skip-selftest      # 只打包
#   ./build.sh --python /path/to/python3
#   ./build.sh --oids oid1,oid2     # 覆盖自检样例
#
# 产物：<脚本目录>/dist/超星文件下载器
#
# Windows 用同目录的 build.ps1，两者参数语义一致。

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python="python3"
skip_selftest=0
no_network=0
oids=""

while [ $# -gt 0 ]; do
  case "$1" in
    --python)       python="$2"; shift 2 ;;
    --skip-selftest) skip_selftest=1; shift ;;
    --no-network)   no_network=1; shift ;;
    --oids)         oids="$2"; shift 2 ;;
    -h|--help)      sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

dist="$here/dist"
work="$here/build"
spec="$here/cx-download.spec"
icon="$here/app.ico"
name="超星文件下载器"
bin_path="$dist/$name"

step() { printf '\n=== %s ===\n' "$1"; }
fail() { printf '[FAIL] %s\n' "$1" >&2; exit 1; }

# ---------- 1. 校验输入 ----------
step "1/5 校验输入"
for f in cx_download_gui.py cx_download.py selftest.py; do
  [ -f "$here/$f" ] || fail "缺少源文件 $f"
  echo "  ok  $f"
done
[ -f "$spec" ] || fail "缺少 spec: $spec"
echo "  ok  cx-download.spec"

# ---------- 2. 图标（仅 macOS 的 .icns 用得上；Linux 的 ELF 不认图标）----------
step "2/5 图标"
if [ -f "$icon" ]; then
  echo "  ok  app.ico 已存在"
elif "$python" -c 'import PIL' >/dev/null 2>&1; then
  echo "  app.ico 不存在，用 make_icon.py 生成"
  "$python" "$here/make_icon.py" "$icon" || fail "make_icon.py 失败"
else
  echo "  跳过：没装 Pillow，且当前平台用不到 .ico（Windows 打包请用 build.ps1）"
fi

# ---------- 3. 打包 ----------
step "3/5 PyInstaller 打包"
rm -rf "$dist" "$work"
"$python" -m PyInstaller --noconfirm --clean \
  --distpath "$dist" --workpath "$work" "$spec" || fail "PyInstaller 打包失败"

[ -f "$bin_path" ] || fail "没有产出 $bin_path"
chmod +x "$bin_path" 2>/dev/null || true
echo "  ok  $bin_path  ($(du -h "$bin_path" | cut -f1))"

# ---------- 4. 冻结自检 ----------
if [ "$skip_selftest" -eq 1 ]; then
  step "4/5 冻结自检（已跳过）"
else
  step "4/5 冻结自检（跑在出货的二进制内部）"
  report="$("$python" -c 'import os,tempfile;print(os.path.join(tempfile.gettempdir(),"cx_download_selftest.txt"))')"
  rm -f "$report"

  args=(--selftest)
  [ "$no_network" -eq 1 ] && args+=(--no-network)
  [ -n "$oids" ] && args+=("$oids")

  set +e
  "$bin_path" "${args[@]}"
  code=$?
  set -e

  if [ -f "$report" ]; then
    sed 's/^/  /' "$report"
  else
    echo "  [警告] 没找到自检报告 $report（报告是唯一线索）" >&2
  fi
  [ "$code" -eq 0 ] || fail "自检未通过（exit=$code），详见 $report"
  echo "  SELFTEST PASS"
fi

# ---------- 5. 完成 ----------
step "5/5 完成"
echo "产物: $bin_path"
cat <<'EOF'
提醒:
  - onefile 每次启动会解压到临时目录，首次慢几秒
  - Linux 需要系统提供 Tk 运行库（libtk8.6 / libtcl8.6），打包机上装了才能收进包
  - 桌面集成可用 packaging/cx-downloader.desktop（按需改 Exec 路径）
EOF
