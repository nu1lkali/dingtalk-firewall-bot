#!/bin/bash
#
# 动态云防火墙 · 启动脚本
# ---------------------------------------------------------------
# v2 起统一入口是根目录 run.py（Web + 内置调度器 + 钉钉长连接，单进程）。
# 本脚本只做环境检查后转发过去，不再自己拼 FLASK_APP。
#
#   ./run.sh            启动服务
#   ./run.sh check      检查依赖与配置
#   ./run.sh test       运行测试套件（不触碰云端）
#   ./run.sh smoke      运行冒烟自检
#

set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[ OK ]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()   { echo -e "${RED}[ERR ]${NC} $1"; }

# 优先使用项目虚拟环境
PYTHON="python3"
if [ -x ".venv/bin/python" ]; then
    PYTHON=".venv/bin/python"
elif [ -x ".venv/Scripts/python.exe" ]; then
    PYTHON=".venv/Scripts/python.exe"
fi

check() {
    info "检查 Python 依赖..."
    local missing=()
    "$PYTHON" -c "import flask" 2>/dev/null || missing+=("flask")
    "$PYTHON" -c "import waitress" 2>/dev/null || missing+=("waitress")
    "$PYTHON" -c "import apscheduler" 2>/dev/null || missing+=("apscheduler")
    "$PYTHON" -c "import yaml" 2>/dev/null || missing+=("pyyaml")
    "$PYTHON" -c "import requests" 2>/dev/null || missing+=("requests")
    "$PYTHON" -c "import alibabacloud_swas_open20200601" 2>/dev/null || missing+=("alibabacloud_swas_open20200601")
    "$PYTHON" -c "import dingtalk_stream" 2>/dev/null || missing+=("dingtalk-stream")

    if [ ${#missing[@]} -eq 0 ]; then
        ok "依赖齐全"
    else
        err "缺少依赖: ${missing[*]}"
        info "安装: $PYTHON -m pip install -r requirements.txt"
        return 1
    fi

    if [ ! -f ".env" ] && [ -f ".env.example" ]; then
        warn ".env 不存在，已从模板复制（旧版配置迁移用，可留空）"
        cp .env.example .env
    fi
    mkdir -p logs data
    ok "目录就绪"
}

case "${1:-start}" in
    check) check ;;
    test)  check && "$PYTHON" scripts/test_suite.py ;;
    smoke) check && "$PYTHON" scripts/smoke_test.py ;;
    start)
        check
        info "启动动态云防火墙（Ctrl+C 停止）..."
        exec "$PYTHON" run.py "${@:2}"
        ;;
    *)
        echo "用法: $0 [start|check|test|smoke]"
        echo "  start  启动服务（默认，可透传参数如 --port 8080）"
        echo "  check  检查依赖"
        echo "  test   运行测试套件"
        echo "  smoke  运行冒烟自检"
        exit 1
        ;;
esac
