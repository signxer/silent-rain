#!/bin/bash
cd "$(dirname "$0")"

# 首次运行自动安装
if [ ! -d "__pycache__" ] && ! python3 -c "import playwright" 2>/dev/null; then
    echo "首次运行，正在安装环境..."
    ./setup.sh
    echo ""
fi

python3 main.py start
