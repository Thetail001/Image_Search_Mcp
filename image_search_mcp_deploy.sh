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

# ---------------------------------------------------------------------------
# 服务单元名：**单一事实来源**
#
# 早先这里硬编码成 `image-search`，而线上跑的是 `image-search-mcp` —— 实测确认：
# 照旧脚本跑既不会重启到真正在跑的服务，还会额外造一个同名的假单元。
# ---------------------------------------------------------------------------
UNIT_NAME="${IMAGE_SEARCH_UNIT:-image-search-mcp}"
UNIT_PATH="/etc/systemd/system/${UNIT_NAME}.service"

# 已有单元默认**不覆盖**。线上那份把凭据直接写在单元里
# （Environment=MCP_AUTH_TOKEN=...），而这个脚本写的是 EnvironmentFile=.env ——
# 覆盖一下凭据就没了，服务起不来。要替换必须显式说，而且会先备份。
REPLACE_UNIT=0
for arg in "$@"; do
    case "$arg" in
        --replace-unit) REPLACE_UNIT=1 ;;
        -h|--help)
            sed -n '2,6p' "$0"
            echo "用法: $0 [--replace-unit]"
            echo "  IMAGE_SEARCH_UNIT=<名字>  覆盖单元名（默认 image-search-mcp）"
            exit 0
            ;;
        *) echo "未知参数：$arg（试 --help）" >&2; exit 2 ;;
    esac
done

# 检查是否已安装
if [ -f ".env" ] && [ -n "$UV_BIN" ]; then
    echo "检测到已有配置和 uv 环境。"
    read -p "是否仅更新服务代码并重启? (y/n, 默认 y): " IS_UPDATE
    IS_UPDATE=${IS_UPDATE:-y}

    if [ "$IS_UPDATE" = "y" ]; then
        echo -e "\n[1/3] 正在升级 image-search-mcp..."
        "$UV_BIN" tool upgrade image-search-mcp || "$UV_BIN" tool install --python 3.12 image-search-mcp

        # 先确认单元真的存在再重启：**这个脚本只升级代码并重启已存在的单元**，
        # 不负责替你猜单元名。旧版本在这里会去重启一个不存在的服务名。
        if ! systemctl cat "${UNIT_NAME}" >/dev/null 2>&1; then
            echo "错误: 找不到服务单元 ${UNIT_NAME}" >&2
            echo "  线上实际用的是 image-search-mcp.service；" >&2
            echo "  若目标机器上叫别的名字，用 IMAGE_SEARCH_UNIT=<名字> 覆盖。" >&2
            exit 1
        fi

        echo "[2/3] 重载并重启服务（${UNIT_NAME}）..."
        sudo systemctl daemon-reload
        sudo systemctl restart "${UNIT_NAME}"

        echo "[3/3] 检查状态..."
        sleep 2
        echo "服务状态: $(sudo systemctl is-active "${UNIT_NAME}")"
        echo "✅ 更新完成！"
        echo "查看日志: journalctl -u ${UNIT_NAME} -f"
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

WRITE_UNIT=1
if sudo test -f "${UNIT_PATH}"; then
    if [ "$REPLACE_UNIT" != "1" ]; then
        WRITE_UNIT=0
        echo "已存在 ${UNIT_PATH}，**不覆盖**。"
        echo "  线上那份把凭据写在单元里（Environment=MCP_AUTH_TOKEN=...），"
        echo "  而本脚本写的是 EnvironmentFile=.env —— 覆盖会把凭据弄丢，服务就起不来了。"
        echo "  确实要替换就加 --replace-unit（会先按时间戳备份）。"
    else
        UNIT_BACKUP="${UNIT_PATH}.bak.$(date +%Y%m%d%H%M%S)"
        sudo cp -a "${UNIT_PATH}" "${UNIT_BACKUP}"
        echo "已备份原单元 → ${UNIT_BACKUP}"
    fi
fi

if [ "$WRITE_UNIT" = "1" ]; then
    sudo bash -c "cat << EOF > ${UNIT_PATH}
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
fi

# 5. 启动服务
echo -e "\n[6/6] 启动并激活服务..."
sudo systemctl daemon-reload
sudo systemctl enable "${UNIT_NAME}"
sudo systemctl restart "${UNIT_NAME}"

# 6. 最终检查
echo "------------------------------------------------"
echo "✅ 部署完成！"
echo "服务状态: $(sudo systemctl is-active "${UNIT_NAME}")"
echo "访问地址: http://$(curl -s ifconfig.me):${PORT}/sse"
echo "Python版本: $($UV_BIN run --python 3.12 python --version)"
echo "------------------------------------------------"
echo "查看实时日志命令: journalctl -u ${UNIT_NAME} -f"