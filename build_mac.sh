#!/bin/bash
# HybridMI-BCI GUI — macOS 一键构建脚本
# 用法: ./build_mac.sh

set -e

echo "=========================================="
echo "  HybridMI-BCI GUI v2.4.14  macOS 构建"
echo "=========================================="

# 检查 Python
if ! command -v python3 &> /dev/null; then
    echo "❌ 未找到 python3，请先安装 Python 3.9+"
    exit 1
fi

echo "✓ Python: $(python3 --version)"

# 创建虚拟环境
if [ ! -d "venv" ]; then
    echo "[1/5] 创建虚拟环境..."
    python3 -m venv venv
fi

# 激活虚拟环境
source venv/bin/activate

# 安装依赖
echo "[2/5] 安装依赖..."
pip install -r requirements.txt

# 生成图标（如果需要）
if [ ! -f "AppIcon.icns" ]; then
    echo "[3/5] 生成 macOS 图标..."
    python3 create_icns.py
fi

# PyInstaller 打包
echo "[4/5] PyInstaller 打包..."
pyinstaller bci_mac.spec --noconfirm

# 完成后提示
echo "[5/5] 完成！"
echo ""
echo "✅ 构建成功！"
echo "📦 应用位置: dist/HybridMI-BCI GUI.app"
echo "💿 可手动制作 DMG 安装包"
echo ""
echo "=========================================="
