#!/bin/bash
# 网络课程自动学习工具 - Mac/Linux 一键安装脚本
set -e

echo "=========================================="
echo "  网络课程自动学习工具 - 环境安装"
echo "=========================================="
echo ""

# 检查 Python
if command -v python3 &>/dev/null; then
    PY=python3
elif command -v python &>/dev/null; then
    PY=python
else
    echo "❌ 未找到 Python，请先安装："
    echo "   Mac:   brew install python3"
    echo "   Linux: sudo apt install python3 python3-pip"
    exit 1
fi

PY_VER=$($PY --version 2>&1)
echo "✓ Python: $PY_VER"

# 安装依赖
echo ""
echo "📦 安装 Python 依赖..."
$PY -m pip install -r requirements.txt --quiet

# 安装 Playwright 内置 Chromium
echo ""
echo "🌐 安装 Playwright 内置 Chromium..."
$PY -m playwright install chromium

echo ""
echo "=========================================="
echo "  ✓ 安装完成！"
echo ""
echo "  运行方式："
echo "    python3 main.py start"
echo "    python3 main.py start --headless"
echo "    python3 main.py start --help"
echo "=========================================="
