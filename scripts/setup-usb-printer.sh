#!/usr/bin/env bash
# 为 HP DeskJet Ink Advantage 4928（4920 系列）创建 USB 队列，并写入官方默认设置。
#
# 用法：
#   sudo ./scripts/setup-usb-printer.sh                 # 自动检测 USB 设备与驱动
#   sudo ./scripts/setup-usb-printer.sh -q MyPrinter    # 指定队列名
#   sudo ./scripts/setup-usb-printer.sh --show          # 只显示检测结果
set -euo pipefail

QUEUE="HP4928"
SHOW_ONLY=0
MODEL_OVERRIDE=""

while [ $# -gt 0 ]; do
  case "$1" in
    -q|--queue) QUEUE="${2:-}"; shift 2 ;;
    -m|--model) MODEL_OVERRIDE="${2:-}"; shift 2 ;;
    --show) SHOW_ONLY=1; shift ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()   { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

for bin in lpinfo lpadmin lpoptions lpstat cupsenable cupsaccept; do
  command -v "$bin" >/dev/null 2>&1 || die "缺少 $bin，请先：sudo apt install -y cups cups-client"
done

if [ "$(id -u)" -ne 0 ]; then
  warn "非 root 运行：需要当前用户在 lpadmin 组（sudo usermod -aG lpadmin \$USER）"
fi

info "检测 USB 打印机…"
DEVICE_URI="$(lpinfo -v 2>/dev/null | awk '/^(direct|network) /{print $2}' | grep -iE '^usb://' | head -n1 || true)"
HPLIP_URI="$(lpinfo -v 2>/dev/null | awk '/^(direct|network) /{print $2}' | grep -iE '^hp:/usb/' | head -n1 || true)"

if [ -z "${DEVICE_URI:-}" ] && [ -z "${HPLIP_URI:-}" ]; then
  warn "没有发现 usb:// 设备。可能原因："
  echo "    1) 打印机没开机 / USB 线没接好（换根线或换个 USB 口试试）"
  echo "    2) 内核没认出来：lsusb | grep -i '03f0'（03f0 是 HP 的厂商号）"
  echo "    3) 被别的驱动占用：dmesg | tail -20"
  echo
  echo "  已连接的 USB 设备（lsusb）："
  lsusb 2>/dev/null | grep -i '03f0' || echo "    （没看到 HP 设备）"
  exit 1
fi
DEVICE_URI="${DEVICE_URI:-$HPLIP_URI}"
info "设备 URI: $DEVICE_URI"

info "查找 HP 驱动…"

# 从设备 URI 里取型号系列号：usb://HP/DeskJet%204900%20series → 4900
# （4928 在 HPLIP 里属于 DeskJet 4900 series，PPD 名是 hp-deskjet_4900_series.ppd）
SERIES="$(printf '%s' "$DEVICE_URI" | sed -E 's/.*(49[0-9]{2}).*/\1/' | grep -E '^49[0-9]{2}$' || true)"
[ -n "$SERIES" ] && info "设备自报系列：$SERIES"

find_model() {
  local pattern="$1" all drv
  all="$(lpinfo -m 2>/dev/null | grep -iE "$pattern" | awk '{print $1}')"
  [ -n "$all" ] || return 0
  # 优先用 CUPS 的 hpcups 驱动 URI
  drv="$(printf '%s\n' "$all" | grep -m1 '^drv:///hpcups\.drv/' || true)"
  printf '%s\n' "${drv:-$(printf '%s\n' "$all" | head -n1)}"
}

MODEL=""
if [ -n "$MODEL_OVERRIDE" ]; then
  MODEL="$MODEL_OVERRIDE"
else
  PATTERNS=()
  [ -n "$SERIES" ] && PATTERNS+=("deskjet_${SERIES}_series\\.ppd")
  PATTERNS+=(
    'deskjet_49[0-9][0-9]_series\.ppd'
    'deskjet_49[0-9][0-9]'
    'deskjet_4[0-9][0-9][0-9]_series\.ppd'
    'deskjet.*_series\.ppd'
  )
  for pattern in "${PATTERNS[@]}"; do
    MODEL="$(find_model "$pattern")"
    [ -n "$MODEL" ] && break
  done
fi

if [ -z "$MODEL" ]; then
  warn "lpinfo -m 里没有找到匹配的 DeskJet PPD。"
  echo "    处理办法（按顺序试）："
  echo "      1) sudo apt install -y hplip hplip-plugin"
  echo "      2) sudo hp-setup -i          # HPLIP 向导式安装，会自动选对 PPD"
  echo "      3) 再跑一次本脚本"
  echo
  echo "    当前可用的 DeskJet 型号（前 20 条）："
  lpinfo -m 2>/dev/null | grep -i 'deskjet' | head -n 20 || true
  echo
  echo "    也可以手动指定：sudo $0 --model 'drv:///hpcups.drv/hp-deskjet_4900_series.ppd'"
  exit 1
fi
info "驱动: $MODEL"

if [ "$SHOW_ONLY" -eq 1 ]; then
  echo "队列名:  $QUEUE"
  echo "设备:    $DEVICE_URI"
  echo "驱动:    $MODEL"
  exit 0
fi

if lpstat -p "$QUEUE" >/dev/null 2>&1; then
  warn "队列 $QUEUE 已存在，更新其设备与驱动"
  lpadmin -p "$QUEUE" -E -v "$DEVICE_URI" -m "$MODEL" \
          -D "HP DeskJet Ink Advantage 4928" -L "USB"
else
  info "创建队列 $QUEUE"
  lpadmin -p "$QUEUE" -E -v "$DEVICE_URI" -m "$MODEL" \
          -D "HP DeskJet Ink Advantage 4928" -L "USB"
fi

cupsenable "$QUEUE"
cupsaccept "$QUEUE"

# ---------------------------------------------------------------------------
# 写入官方默认设置：只设置驱动真正支持的选项，避免 CUPS 记录无效设置
#
# 注意：`lpoptions -o` 只写当前用户的 ~/.cups/lpoptions，服务跑在 hp4928 用户下
# 读不到 root 的这份；所以这里用 `lpadmin -o` 写进队列 PPD，成为系统级默认值。
# ---------------------------------------------------------------------------
info "写入默认打印设置（黑白 / A4 / 单面 / 600dpi）"
SUPPORTED="$(lpoptions -p "$QUEUE" -l 2>/dev/null || true)"

supports() {
  printf '%s\n' "$SUPPORTED" \
    | awk -F: '{print $1}' \
    | cut -d/ -f1 \
    | tr -d ' ' \
    | grep -qx "$1"
}

set_opt() {
  local name="$1" value="$2" label="${3:-$1}"
  if supports "$name"; then
    if lpadmin -p "$QUEUE" -o "$name=$value" >/dev/null 2>&1; then
      echo "    ✓ $label = $value"
    else
      echo "    ! $label = $value 设置失败（驱动可能不接受该取值）"
    fi
  else
    echo "    - 跳过 $label（该驱动没有 $name 选项）"
  fi
}

# 纸张：A4（官方支持尺寸）
if supports "PageSize"; then
  set_opt PageSize A4 "纸张尺寸"
elif supports "media"; then
  set_opt media A4 "纸张尺寸"
fi
set_opt MediaType Plain "纸张类型"
set_opt InputSlot Auto "纸张来源"
set_opt Resolution 600dpi "打印质量"
set_opt ColorMode Gray "色彩（黑白）"
set_opt Duplex None "驱动双面（该机型无自动双面，保持关闭）"

# ---------------------------------------------------------------------------
info "验证"
lpstat -p "$QUEUE" -l || true
echo
echo "当前有效选项："
lpoptions -p "$QUEUE" -l 2>/dev/null | sed 's/^/    /' | head -n 40 || true
echo
cat <<EOF
完成。后续：
  1) 打印测试页：  lp -d $QUEUE /usr/share/cups/data/testprint
  2) 服务里配置队列名：printer.name: $QUEUE
  3) 手动双面由本服务实现（先打奇数页 → 点「继续打印」→ 打偶数页），
     不要开启驱动里的 Duplex，该机型没有自动双面器。
EOF
