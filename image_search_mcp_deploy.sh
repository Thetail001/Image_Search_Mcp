#!/bin/bash

# ==========================================
# Image Search MCP Server 一键部署脚本 (UV 引擎版)
# ==========================================

set -e

echo "------------------------------------------------"
echo "Image Search MCP Server 部署与更新脚本"
echo "------------------------------------------------"

# 寻找 uv 可执行文件路径的函数
find_uv() {
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    local uv_path=$(which uv || echo "")
    if [ -z "$uv_path" ]; then
        if [ -f "$HOME/.local/bin/uv" ]; then uv_path="$HOME/.local/bin/uv";
        elif [ -f "$HOME/.cargo/bin/uv" ]; then uv_path="$HOME/.cargo/bin/uv"; fi
    fi
    echo "$uv_path"
}

UV_BIN=$(find_uv)

# 检查是否已安装
if [ -f ".env" ] && [ -n "$UV_BIN" ]; then
    echo "检测到已有配置和 uv 环境。"
    read -p "是否仅更新服务代码并重启? (y/n, 默认 y): " IS_UPDATE
    IS_UPDATE=${IS_UPDATE:-y}

    if [ "$IS_UPDATE" = "y" ]; then
        echo -e "\n[1/3] 正在升级 image-search-mcp..."
        "$UV_BIN" tool upgrade image-search-mcp || "$UV_BIN" tool install --python 3.12 image-search-mcp
        
        echo "[2/3] 重载并重启服务..."
        sudo systemctl restart image-search
        
        echo "[3/3] 检查状态..."
        sleep 2
        echo "服务状态: $(sudo systemctl is-active image-search)"
        echo "✅ 更新完成！"
        echo "查看日志: journalctl -u image-search -f"
        exit 0
    fi
fi

# 1. 交互式收集配置
echo "[1/6] 配置运行参数:"
# 尝试从旧 .env 读取默认值
[ -f .env ] && source .env || true

read -p "请输入服务监听端口 (当前: ${PORT:-8000}): " NEW_PORT
PORT=${NEW_PORT:-${PORT:-8000}}

echo -e "\n[2/6] 配置环境变量 (直接按回车保留当前值或跳过):"
read -p "请输入 MCP_AUTH_TOKEN: " NEW_AUTH_TOKEN
MCP_AUTH_TOKEN=${NEW_AUTH_TOKEN:-${MCP_AUTH_TOKEN}}

read -p "请输入 SauceNAO API Key: " NEW_SAUCE_KEY
IMAGE_SEARCH_API_KEY=${NEW_SAUCE_KEY:-${IMAGE_SEARCH_API_KEY}}

read -p "请输入通用 Cookies: " NEW_COOKIES
IMAGE_SEARCH_COOKIES=${NEW_COOKIES:-${IMAGE_SEARCH_COOKIES}}

read -p "请输入 HTTP 代理: " NEW_PROXY
IMAGE_SEARCH_PROXY=${NEW_PROXY:-${IMAGE_SEARCH_PROXY}}

# 写入当前目录下的 .env 文件
cat << EOF > .env
# 基础运行配置
HOST=0.0.0.0
PORT=${PORT}
MCP_AUTH_TOKEN=${MCP_AUTH_TOKEN}

# 搜图引擎可选配置
IMAGE_SEARCH_API_KEY=${IMAGE_SEARCH_API_KEY}
IMAGE_SEARCH_COOKIES=${IMAGE_SEARCH_COOKIES}
IMAGE_SEARCH_PROXY=${IMAGE_SEARCH_PROXY}
EOF

chmod 600 .env
echo -e "\n配置已保存至 .env 文件。"

# 2. 安装基础工具
echo -e "\n[3/6] 安装基础工具 (curl)..."
sudo apt update
sudo apt install -y curl ca-certificates

# 3. 安装 uv 并通过 uv 安装 Python 3.12
echo -e "\n[4/6] 安装 uv 并自动配置 Python 3.12..."
curl -LsSf https://astral.sh/uv/install.sh | sh

# 刷新环境变量，包含可能的 uv 安装路径
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

# 寻找 uv 可执行文件路径
UV_BIN=$(which uv || echo "")
if [ -z "$UV_BIN" ]; then
    if [ -f "$HOME/.local/bin/uv" ]; then
        UV_BIN="$HOME/.local/bin/uv"
    elif [ -f "$HOME/.cargo/bin/uv" ]; then
        UV_BIN="$HOME/.cargo/bin/uv"
    else
        echo "错误: 无法找到 uv 命令，请检查安装是否成功。"
        exit 1
    fi
fi
echo "使用 uv 路径: $UV_BIN"

# 使用 uv 安装独立的 Python 3.12 (不依赖 apt 仓库)
"$UV_BIN" python install 3.12

# 4. 创建 Systemd 服务
echo -e "\n[5/6] 创建 Systemd 服务..."
CURRENT_USER=$(whoami)
CURRENT_DIR=$(pwd)

sudo bash -c "cat << EOF > /etc/systemd/system/image-search.service
[Unit]
Description=Image Search MCP Server
After=network.target

[Service]
User=$CURRENT_USER
WorkingDirectory=$CURRENT_DIR
EnvironmentFile=$CURRENT_DIR/.env
# 使用 uv 运行托管的工具，并强制指定 Python 3.12
ExecStart=$UV_BIN tool run --python 3.12 image-search-mcp --sse --host 0.0.0.0 --port ${PORT}
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF"

# 5. 启动服务
echo -e "\n[6/6] 启动并激活服务..."
sudo systemctl daemon-reload
sudo systemctl enable image-search
sudo systemctl restart image-search

# 6. 最终检查
echo "------------------------------------------------"
echo "✅ 部署完成！"
echo "服务状态: $(sudo systemctl is-active image-search)"
echo "访问地址: http://$(curl -s ifconfig.me):${PORT}/sse"
echo "Python版本: $($UV_BIN run --python 3.12 python --version)"
echo "------------------------------------------------"
echo "查看实时日志命令: journalctl -u image-search -f"