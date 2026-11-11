#!/bin/zsh
set -eu
cd -- "$(dirname -- "$0")"
if [[ ! -x .venv/bin/python ]]; then
  print '缺少本项目的 Python 环境，请按 README 安装依赖。'
  read -r '?按回车退出'
  exit 1
fi
export PORT="${PORT:-18772}"
print "轨道内容工作台：http://localhost:${PORT}"
print '关闭此窗口或按 Control+C 停止服务。'
exec .venv/bin/python studio.py
