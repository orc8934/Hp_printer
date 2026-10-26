#!/usr/bin/env bash
# HP 4928 打印服务 · Debian/Ubuntu 一键安装
#
#   sudo ./scripts/install.sh                  # 安装并启动（默认 0.0.0.0:10086）
#   sudo ./scripts/install.sh --port 8080
#   sudo ./scripts/install.sh --no-office      # 不装 LibreOffice（Word 转换不可用）
#   sudo ./scripts/install.sh --no-printer     # 不创建 CUPS 队列（稍后手动配）
#   sudo ./scripts/install.sh --uninstall
set -euo pipefail

APP_NAME="hp4928-print"
INSTALL_DIR="/opt/hp4928-print"
CONFIG_DIR="/etc/hp4928-print"
DATA_DIR="/var/lib/hp4928-print"
SERVICE_USER="hp4928"
PORT=10086
WITH_OFFICE=1
WITH_PRINTER=1
DO_UNINSTALL=0

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="${2:?需要端口号}"; shift 2 ;;
    --no-office) WITH_OFFICE=0; shift ;;
    --no-printer) WITH_PRINTER=0; shift ;;
    --uninstall) DO_UNINSTALL=1; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 root 运行：sudo $0 $*"
command -v apt-get >/dev/null 2>&1 || die "只支持 Debian/Ubuntu（需要 apt-get）"

# 以服务用户身份执行命令（不假设安装了 sudo）
as_user() {
  if command -v runuser >/dev/null 2>&1; then
    runuser -u "$SERVICE_USER" -- "$@"
  else
    sudo -u "$SERVICE_USER" "$@"
  fi
}

# ---------------------------------------------------------------------------
if [ "$DO_UNINSTALL" -eq 1 ]; then
  warn "将停止并删除服务、程序与配置。"
  read -r -p "数据目录 $DATA_DIR 里的打印记录也要删除吗？[y/N] " answer
  systemctl disable --now "$APP_NAME" 2>/dev/null || true
  rm -f "/etc/systemd/system/$APP_NAME.service" /usr/local/bin/hp4928
  systemctl daemon-reload
  rm -rf "$INSTALL_DIR" "$CONFIG_DIR"
  if [[ "${answer:-n}" =~ ^[Yy]$ ]]; then
    rm -rf "$DATA_DIR"
    ok "已删除数据目录"
  else
    info "保留数据目录：$DATA_DIR"
  fi
  if id "$SERVICE_USER" >/dev/null 2>&1; then
    userdel "$SERVICE_USER" 2>/dev/null || true
  fi
  ok "卸载完成（CUPS 队列 $APP_NAME 未删除，可用 lpadmin -x <队列名> 手动删）"
  exit 0
fi

# ---------------------------------------------------------------------------
info "安装系统依赖（apt）"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq || warn "apt-get update 失败，继续尝试安装（可能使用旧索引）"
BASE_PACKAGES=(
  python3 python3-venv python3-pip
  cups cups-client cups-filters cups-ipp-utils
  hplip
  poppler-utils
  fonts-noto-cjk fontconfig
  ca-certificates curl rsync
)
apt-get install -y -qq \
  -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" \
  "${BASE_PACKAGES[@]}"
ok "基础依赖已安装"

if [ "$WITH_OFFICE" -eq 1 ]; then
  info "安装 LibreOffice Writer（Word 转 PDF 必需，约 300 MB）"
  if apt-get install -y -qq libreoffice-writer; then
    ok "LibreOffice 已安装"
  else
    warn "LibreOffice 安装失败，Word 文档将无法转换（PDF/图片不受影响）"
  fi
else
  warn "跳过 LibreOffice：只支持 PDF 与图片"
fi

info "启用 CUPS 服务"
systemctl enable --now cups >/dev/null 2>&1 || warn "CUPS 启动失败，请检查 systemctl status cups"
ok "CUPS 已就绪"

# ---------------------------------------------------------------------------
info "创建系统用户 $SERVICE_USER"
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
  ok "用户已创建"
else
  ok "用户已存在"
fi
# 打印本身不需要 lpadmin，创建队列/改队列才需要
usermod -aG lpadmin "$SERVICE_USER" 2>/dev/null || true

info "部署程序到 $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
if [ "$SRC_DIR" -ef "$INSTALL_DIR" ]; then
  ok "已在目标目录运行，跳过文件复制"
else
  # 只替换程序目录，保留 .venv / data / 用户自己的 config.local.yaml
  rm -rf "$INSTALL_DIR/hp4928" "$INSTALL_DIR/scripts" "$INSTALL_DIR/deploy"
  rsync -a --exclude '__pycache__' \
    "$SRC_DIR/hp4928" "$SRC_DIR/scripts" "$SRC_DIR/deploy" "$INSTALL_DIR/"
  install -m 644 "$SRC_DIR/run.py" "$SRC_DIR/requirements.txt" "$INSTALL_DIR/"
  for extra in README.md config.example.yaml; do
    if [ -f "$SRC_DIR/$extra" ]; then
      install -m 644 "$SRC_DIR/$extra" "$INSTALL_DIR/$extra"
    fi
  done
  ok "程序已部署"
fi
chmod +x "$INSTALL_DIR"/scripts/*.sh 2>/dev/null || true
chown -R root:root "$INSTALL_DIR"
chmod -R a+rX "$INSTALL_DIR"

info "创建虚拟环境并安装 Python 依赖"
if [ ! -x "$INSTALL_DIR/.venv/bin/python" ]; then
  python3 -m venv "$INSTALL_DIR/.venv"
fi
"$INSTALL_DIR/.venv/bin/python" -m pip install --upgrade pip wheel >/dev/null
"$INSTALL_DIR/.venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
ok "虚拟环境就绪：$INSTALL_DIR/.venv"

info "安装命令行工具 /usr/local/bin/hp4928"
cat > /usr/local/bin/hp4928 <<LAUNCH
#!/bin/sh
# 由 hp4928-print 安装脚本生成：
#   1) 固定配置/数据目录，避免数据落到只读的 /opt
#   2) 固定 PYTHONPATH，避免 "No module named hp4928"
#   3) root 调用时自动降权到 $SERVICE_USER，避免数据文件变成 root 所有
export HP4928_CONFIG="$CONFIG_DIR/config.yaml"
export HP4928_DATA_DIR="$DATA_DIR"
export PYTHONPATH="$INSTALL_DIR"
PY="$INSTALL_DIR/.venv/bin/python"
if [ "\$(id -u)" -eq 0 ] && command -v runuser >/dev/null 2>&1 && id $SERVICE_USER >/dev/null 2>&1; then
  exec runuser -u $SERVICE_USER -- "\$PY" -m hp4928.cli "\$@"
fi
exec "\$PY" -m hp4928.cli "\$@"
LAUNCH
chmod 755 /usr/local/bin/hp4928
ok "以后可直接用：hp4928 doctor / hp4928 passwd --user admin"

# ---------------------------------------------------------------------------
info "准备配置与数据目录"
mkdir -p "$CONFIG_DIR" "$DATA_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"
chmod 750 "$DATA_DIR"

if [ ! -f "$CONFIG_DIR/config.yaml" ]; then
  cat > "$CONFIG_DIR/config.yaml" <<EOF
# HP 4928 打印服务配置（由 install.sh 生成）
paths:
  # 必须落在可写路径：systemd 单元用 ProtectSystem=strict 只放开这个目录
  data_dir: "$DATA_DIR"

server:
  host: "0.0.0.0"        # 局域网可访问；只想本机用改成 127.0.0.1
  port: $PORT
  threads: 8
  session_hours: 12
  max_upload_mb: 64

printer:
  name: "HP4928"          # 与 setup-usb-printer.sh 创建的队列名一致
  backend: "auto"         # auto | cups | virtual
  usb:
    require_device: true  # USB 掉线时自动降级为虚拟打印机
    allow_network_fallback: true

print:
  media: A4
  color: mono             # 默认黑白
  duplex: "off"           # 默认单面；long-edge/short-edge 为手动双面
  quality: normal
  photo:
    media: "4x6in"        # 6 寸相纸（4R 102×152mm）
    dpi: 300
    border_mm: 3.0        # 该机型不支持无边距

conversion:
  timeout: 180

security:
  force_password_change: false
  login_max_attempts: 5
  login_window_seconds: 300

retention:
  days: 14
  keep_failed_days: 30

logging:
  level: INFO
EOF
  ok "已生成 $CONFIG_DIR/config.yaml"
else
  ok "沿用已有配置 $CONFIG_DIR/config.yaml"
fi
chmod 640 "$CONFIG_DIR/config.yaml"
chown root:"$SERVICE_USER" "$CONFIG_DIR/config.yaml"

if [ ! -f "$CONFIG_DIR/env" ]; then
  cat > "$CONFIG_DIR/env" <<'EOF'
# 环境变量覆盖（systemd EnvironmentFile）。取消注释即可生效。
# HP4928_BACKEND=auto
# HP4928_LOG_LEVEL=INFO
# HP4928_TRUST_PROXY=false
# 放在 Nginx 后面并启用 HTTPS 时：
# HP4928_BEHIND_HTTPS=true
# 会话密钥（不设则自动生成在数据目录 secret_key）
# HP4928_SECRET_KEY=
EOF
  chmod 640 "$CONFIG_DIR/env"
  ok "已生成 $CONFIG_DIR/env"
fi

info "安装 systemd 服务"
install -m 644 "$INSTALL_DIR/deploy/$APP_NAME.service" "/etc/systemd/system/$APP_NAME.service"
systemctl daemon-reload
systemctl enable "$APP_NAME" >/dev/null

# ---------------------------------------------------------------------------
info "初始化数据库与默认账号"
# 注意：python -m 只从当前目录找包，必须 cd 到程序目录；PYTHONPATH 再加一层保险
if ( cd "$INSTALL_DIR" && as_user env PYTHONPATH="$INSTALL_DIR" HP4928_DATA_DIR="$DATA_DIR" \
       "$INSTALL_DIR/.venv/bin/python" -m hp4928.cli init --config "$CONFIG_DIR/config.yaml" ); then
  ok "初始化完成"
else
  warn "初始化失败，请手动执行："
  echo "     sudo -u $SERVICE_USER env PYTHONPATH=$INSTALL_DIR HP4928_DATA_DIR=$DATA_DIR \\"
  echo "       $INSTALL_DIR/.venv/bin/python -m hp4928.cli init --config $CONFIG_DIR/config.yaml"
fi

if [ "$WITH_PRINTER" -eq 1 ]; then
  info "创建 CUPS USB 队列"
  if "$INSTALL_DIR/scripts/setup-usb-printer.sh" --queue HP4928; then
    ok "CUPS 队列就绪"
  else
    warn "未能自动创建队列（USB 没插好或缺少 HPLIP）。"
    echo "    处理办法：插好 USB 后执行  sudo $INSTALL_DIR/scripts/setup-usb-printer.sh"
    echo "    服务会先以降级模式（虚拟打印机）运行，不影响使用与调试。"
  fi
fi

info "启动服务"
systemctl restart "$APP_NAME"
sleep 2
if systemctl is-active --quiet "$APP_NAME"; then
  ok "服务已启动"
else
  warn "服务未能启动，请查看：journalctl -u $APP_NAME -n 50 --no-pager"
fi

# ---------------------------------------------------------------------------
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -qi 'Status: active'; then
  info "检测到 ufw 已启用，放行端口 $PORT"
  ufw allow "$PORT/tcp" >/dev/null && ok "已放行 $PORT/tcp"
fi

IP_ADDR="$(hostname -I 2>/dev/null | awk '{print $1}')"

cat <<EOF

────────────────────────────────────────────────────────────
 安装完成
────────────────────────────────────────────────────────────
 访问地址：  http://${IP_ADDR:-<服务器IP>}:$PORT/
 本机访问：  http://127.0.0.1:$PORT/

 默认账号（首次运行自动生成，登录后请修改）：
EOF

CRED_FILE="$DATA_DIR/initial-admin.txt"
if [ -f "$CRED_FILE" ]; then
  grep -E '^(用户名|密码)' "$CRED_FILE" | sed 's/^/   /'
  echo "   凭据文件：$CRED_FILE（改完密码后建议删除）"
elif [ -f "$DATA_DIR/hp4928.sqlite3" ]; then
  echo "   （账号已存在，未重新生成。重置：sudo -u $SERVICE_USER env PYTHONPATH=$INSTALL_DIR HP4928_DATA_DIR=$DATA_DIR $INSTALL_DIR/.venv/bin/python -m hp4928.cli passwd --user admin --config $CONFIG_DIR/config.yaml）"
else
  echo "   ⚠ 尚未初始化成功（没有账号，也没有数据库）。请执行："
  echo "     sudo -u $SERVICE_USER env PYTHONPATH=$INSTALL_DIR HP4928_DATA_DIR=$DATA_DIR \\"
  echo "       $INSTALL_DIR/.venv/bin/python -m hp4928.cli init --config $CONFIG_DIR/config.yaml"
fi

cat <<EOF

 常用命令：
   systemctl status $APP_NAME
   journalctl -u $APP_NAME -f
   hp4928 doctor                      # 环境自检
   hp4928 passwd --user admin         # 重置密码
   sudo $INSTALL_DIR/scripts/setup-usb-printer.sh

 配置：$CONFIG_DIR/config.yaml    数据：$DATA_DIR
 修改配置后：sudo systemctl restart $APP_NAME
EOF
