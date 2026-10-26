#!/usr/bin/env bash
# 创建虚拟环境并安装依赖（手动部署时用；install.sh 会调用等价步骤）
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
ROOT="$(pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-$ROOT/.venv}"

echo "==> 项目目录: $ROOT"
echo "==> Python: $($PYTHON_BIN -V 2>&1)"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "找不到 $PYTHON_BIN，请先安装：sudo apt install -y python3 python3-venv" >&2
  exit 1
fi

"$PYTHON_BIN" -c 'import venv, ensurepip' 2>/dev/null || {
  echo "缺少 venv 模块，请执行：sudo apt install -y python3-venv" >&2
  exit 1
}

if [ ! -d "$VENV_DIR" ]; then
  echo "==> 创建虚拟环境 $VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
else
  echo "==> 复用已有虚拟环境 $VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install --upgrade pip wheel >/dev/null
"$VENV_DIR/bin/pip" install -r "$ROOT/requirements.txt"

echo
echo "==> 依赖安装完成"
"$VENV_DIR/bin/python" - <<'PY'
import flask, waitress, pypdf, PIL, yaml
print(f"Flask {flask.__version__} / waitress {waitress.__version__} / "
      f"pypdf {pypdf.__version__} / Pillow {PIL.__version__}")
PY
cat <<EOF

下一步：
  source .venv/bin/activate
  python -m hp4928.cli init      # 初始化数据库与默认账号
  python -m hp4928.cli doctor    # 环境自检
  python -m hp4928.cli serve     # 启动服务（0.0.0.0:10086）
EOF
