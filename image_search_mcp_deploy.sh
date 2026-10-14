#!/bin/bash
# 使用 uv 和 systemd 部署或更新 Image Search MCP。
#
# --update-only：升级工具并重启现有单元，不修改配置。
# --reconfigure：备份并重写 .env，保留非托管键。
# --replace-unit：重新配置，同时备份并替换 systemd 单元。
#
# 默认保留现有单元；启动后检查服务状态、健康端点和可用的认证令牌。

set -uo pipefail
umask 077

ENV_PATH="$(pwd)/.env"

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

find_uv() {
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    local uv_path
    uv_path=$(command -v uv || echo "")
    if [ -z "$uv_path" ]; then
        if [ -f "$HOME/.local/bin/uv" ]; then uv_path="$HOME/.local/bin/uv";
        elif [ -f "$HOME/.cargo/bin/uv" ]; then uv_path="$HOME/.cargo/bin/uv"; fi
    fi
    echo "$uv_path"
}

UV_BIN=$(find_uv)

fail() { echo "错误: $*" >&2; exit 1; }

# 写成 systemd EnvironmentFile 支持的单行值。
env_quote() {
    local value="$1"
    [[ "$value" != *$'\n'* && "$value" != *$'\r'* ]] || return 1
    case "$value" in
        *[[:space:]]*|*['"'\''\\$`#']*)
            value="${value//\\/\\\\}"
            value="${value//\"/\\\"}"
            printf '"%s"' "$value"
            ;;
        *) printf '%s' "$value" ;;
    esac
}

# 只读取部署脚本所需的键；不执行 .env 中的变量展开或命令替换。
# 支持单行未引用值、单引号值和双引号值。多行值需要人工调整。
load_env() {
    local line key value first char next decoded i line_number=0
    while IFS= read -r line || [[ -n "$line" ]]; do
        ((line_number += 1))
        line="${line%$'\r'}"
        [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=(.*)$ ]] || continue
        key="${BASH_REMATCH[1]}"
        value="${BASH_REMATCH[2]}"
        case "$key" in
            HOST|PORT|MCP_AUTH_TOKEN|IMAGE_SEARCH_API_KEY|IMAGE_SEARCH_PROXY) ;;
            *) continue ;;
        esac
        value="${value#"${value%%[![:space:]]*}"}"
        value="${value%"${value##*[![:space:]]}"}"
        first="${value:0:1}"
        if [[ "$first" == "'" || "$first" == '"' ]]; then
            [[ ${#value} -ge 2 && "${value: -1}" == "$first" ]] \
                || fail ".env 第 ${line_number} 行的引号未闭合；仅支持单行值"
            value="${value:1:${#value}-2}"
        fi
        decoded=""
        for ((i = 0; i < ${#value}; i++)); do
            char="${value:i:1}"
            if [[ "$char" == "$first" && ( "$first" == "'" || "$first" == '"' ) ]]; then
                fail ".env 第 ${line_number} 行含未转义的引号"
            fi
            if [[ "$char" == '\' && "$first" != "'" ]]; then
                ((i += 1))
                [[ $i -lt ${#value} ]] || fail ".env 第 ${line_number} 行含不完整的转义"
                next="${value:i:1}"
                if [[ "$first" == '"' ]]; then
                    case "$next" in
                        '\'|'"'|'$'|'`') char="$next" ;;
                        *) char="\$next" ;;
                    esac
                else
                    char="$next"
                fi
            fi
            decoded+="$char"
        done
        printf -v "$key" '%s' "$decoded"
    done < "$ENV_PATH"
}

# ---------------------------------------------------------------------------
# 部署与更新使用同一个可配置单元名。
#
# ---------------------------------------------------------------------------

UNIT_NAME="${IMAGE_SEARCH_UNIT:-image-search-mcp}"
# 单元目录留一个覆盖点（默认值不变）：门禁要在不碰真 /etc/systemd 的前提下跑完整条
# 配置流程 —— 否则验这条门禁就得往真系统里写单元文件，那比脚本自身的 bug 更糟。
SYSTEMD_DIR="${IMAGE_SEARCH_SYSTEMD_DIR:-/etc/systemd/system}"
UNIT_PATH="${SYSTEMD_DIR}/${UNIT_NAME}.service"

MODE=""
REPLACE_UNIT=0

usage() {
    cat <<'USAGE'
用法: image_search_mcp_deploy.sh [选项]

  （无选项）        已有 .env 时询问；y = 仅升级包并重启，n = 走配置流程
  --reconfigure     直接走配置流程：备份并重写 .env，保留非托管键
  --replace-unit    连同 systemd 单元一起替换（隐含配置流程，旧单元先备份）
  -h, --help        显示本说明

环境变量:
  IMAGE_SEARCH_UNIT=<名字>      覆盖单元名（默认 image-search-mcp）
  IMAGE_SEARCH_SYSTEMD_DIR=...  覆盖单元目录（默认 /etc/systemd/system；主要给门禁用）

注意: 已有单元默认不覆盖 —— 线上那份把凭据写在单元里，覆盖会弄丢凭据。
USAGE
}

for arg in "$@"; do
    case "$arg" in
        --update-only)   MODE="update" ;;
        --reconfigure)   MODE="reconfigure" ;;
        --replace-unit)  MODE="reconfigure"; REPLACE_UNIT=1 ;;
        -h|--help)       usage; exit 0 ;;
        *) echo "未知参数：$arg（试 --help）" >&2; exit 2 ;;
    esac
done

# 分别检查进程状态、HTTP 健康和认证令牌。
#
verify_service() {
    # 显式查退出码。旧版本把这句塞进 $( )，非零状态被 echo 本身的状态掩盖。
    if ! sudo systemctl is-active --quiet "${UNIT_NAME}"; then
        echo "错误: ${UNIT_NAME} 没有处于 active 状态" >&2
        sudo systemctl status "${UNIT_NAME}" --no-pager -l 2>&1 | tail -20 >&2 || true
        return 1
    fi
    echo "服务状态: active"

    local port="${PORT:-}"
    if [ -z "$port" ] && [ -f "$ENV_PATH" ]; then
        port=$(grep -m1 '^PORT=' "$ENV_PATH" | cut -d= -f2- | tr -d '"')
    fi
    port="${port:-8000}"
    local base="http://127.0.0.1:${port}"
    local code

    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "${base}/healthz" || true)
    if [ "$code" != "200" ]; then
        echo "错误: /healthz 返回 ${code:-无响应}（期望 200）" >&2
        return 1
    fi
    echo "健康检查: /healthz 200"

    # 带凭据的请求才证明凭据装对了。/messages/ 需要鉴权：
    # 有效 token 因缺 session_id 回 400，无效 token 回 401 —— 用这个区分。
    local token="${MCP_AUTH_TOKEN:-}"
    if [ -z "$token" ]; then
        echo "警告: 拿不到 MCP_AUTH_TOKEN（线上单元可能把凭据写在单元里），本次跳过鉴权冒烟" >&2
        return 0
    fi
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
        -X POST -H "Authorization: Bearer ${token}" -H 'Content-Type: application/json' \
        -d '{}' "${base}/messages/" || true)
    case "$code" in
        401) echo "错误: 带凭据请求仍返回 401 —— 凭据没装对" >&2; return 1 ;;
        400) echo "鉴权冒烟: 凭据被接受（400 = 缺 session_id，符合预期）" ;;
        000|'') echo "错误: 带凭据请求无响应" >&2; return 1 ;;
        *) echo "错误: 鉴权检查返回 ${code}（期望 400），无法确认凭据有效" >&2; return 1 ;;
    esac
    return 0
}

echo "------------------------------------------------"
echo "Image Search MCP Server 部署与更新脚本"
echo "------------------------------------------------"

# ---------------------------------------------------------------------------
# 1. 前置探测（只读，不做任何写入）
# ---------------------------------------------------------------------------

echo "单元名: ${UNIT_NAME}"
echo "配置文件: ${ENV_PATH}$([ -f "$ENV_PATH" ] && echo "（已存在）" || echo "（不存在）")"

LIVE_EXEC=""
UNIT_EXISTS=0
if sudo test -f "${UNIT_PATH}"; then
    UNIT_EXISTS=1
    echo "已存在单元: ${UNIT_PATH}"
    # 显示现有 ExecStart，便于核对升级工具是否为服务实际使用的入口。
    LIVE_EXEC=$(sudo systemctl cat "${UNIT_NAME}" 2>/dev/null | grep -m1 '^ExecStart=' || true)
    [ -n "$LIVE_EXEC" ] && echo "线上启动方式: ${LIVE_EXEC}"
else
    echo "未发现单元: ${UNIT_PATH}（将创建）"
fi

if [ "$UNIT_EXISTS" = "1" ] && [ "$REPLACE_UNIT" != "1" ]; then
    echo "单元将被保留（要替换就加 --replace-unit，会先备份）。"
fi

# ---------------------------------------------------------------------------
# 2. 决定模式
# ---------------------------------------------------------------------------

if [ -z "$MODE" ]; then
    if [ -f "$ENV_PATH" ] && [ -n "$UV_BIN" ]; then
        read -r -p "是否仅升级服务代码并重启?（y=只升级，不动配置；n=走配置流程）(y/n, 默认 y): " IS_UPDATE
        IS_UPDATE=${IS_UPDATE:-y}
        if [ "$IS_UPDATE" = "y" ]; then MODE="update"; else MODE="reconfigure"; fi
    else
        MODE="reconfigure"
    fi
fi

if [ "$MODE" = "update" ]; then
    echo "本次动作: 仅升级包并重启（不动配置）"
else
    echo "本次动作: 配置流程$([ "$REPLACE_UNIT" = "1" ] && echo "（含替换单元）" || echo "（保留现有单元）")"
fi

# ---------------------------------------------------------------------------
# 3. 仅升级模式
# ---------------------------------------------------------------------------

if [ "$MODE" = "update" ]; then
    [ -n "$UV_BIN" ] || fail "找不到 uv 命令，无法升级"
    # 只为读端口/凭据做校验，不写回。
    [ -f "$ENV_PATH" ] && load_env

    echo -e "\n[1/3] 正在升级 image-search-mcp..."
    if ! "$UV_BIN" tool upgrade image-search-mcp; then
        echo "  upgrade 失败，尝试全新安装..." >&2
        "$UV_BIN" tool install --python 3.12 image-search-mcp \
            || fail "uv tool upgrade/install 都失败了"
    fi

    # 只升级代码并重启已存在的单元：不替你猜单元名。
    if ! sudo systemctl cat "${UNIT_NAME}" >/dev/null 2>&1; then
        fail "找不到服务单元 ${UNIT_NAME}。线上实际用的是 image-search-mcp.service；若目标机器上叫别的名字，用 IMAGE_SEARCH_UNIT=<名字> 覆盖。"
    fi
    if [ -n "$LIVE_EXEC" ] && ! printf '%s' "$LIVE_EXEC" | grep -q 'image-search-mcp'; then
        echo "警告: 线上 ExecStart 看起来不是本脚本管理的入口：" >&2
        echo "  ${LIVE_EXEC}" >&2
        echo "  刚升级的包可能不是服务真正运行的那个，请人工确认。" >&2
    fi

    echo "[2/3] 重载并重启服务（${UNIT_NAME}）..."
    sudo systemctl daemon-reload || fail "systemctl daemon-reload 失败"
    sudo systemctl restart "${UNIT_NAME}" || fail "systemctl restart ${UNIT_NAME} 失败"

    echo "[3/3] 校验..."
    sleep 2
    verify_service || exit 1
    echo "✅ 更新完成！"
    echo "查看日志: journalctl -u ${UNIT_NAME} -f"
    exit 0
fi

# ---------------------------------------------------------------------------
# 4. 配置流程
# ---------------------------------------------------------------------------

echo -e "\n[1/6] 配置运行参数:"

if [ -f "$ENV_PATH" ]; then
    ENV_BACKUP="${ENV_PATH}.bak.$(date +%Y%m%d%H%M%S)"
    cp -a "$ENV_PATH" "$ENV_BACKUP" || fail "备份 ${ENV_PATH} 失败"
    echo "已备份原配置 → ${ENV_BACKUP}"
    load_env
fi

read -r -p "请输入服务监听端口 (当前: ${PORT:-8000}): " NEW_PORT
PORT=${NEW_PORT:-${PORT:-8000}}
case "$PORT" in
    ''|*[!0-9]*) fail "端口必须是数字：${PORT}" ;;
esac
if [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
    fail "端口超出 1-65535：${PORT}"
fi

echo -e "\n[2/6] 配置环境变量 (直接按回车保留当前值或跳过):"
read -r -p "请输入 MCP_AUTH_TOKEN: " NEW_AUTH_TOKEN
MCP_AUTH_TOKEN=${NEW_AUTH_TOKEN:-${MCP_AUTH_TOKEN:-}}
[[ -n "${MCP_AUTH_TOKEN//[[:space:]]/}" ]] || fail "MCP_AUTH_TOKEN 不能为空"

read -r -p "请输入 SauceNAO API Key: " NEW_SAUCE_KEY
IMAGE_SEARCH_API_KEY=${NEW_SAUCE_KEY:-${IMAGE_SEARCH_API_KEY:-}}

read -r -p "请输入 HTTP 代理（留空=不修改）: " NEW_PROXY
IMAGE_SEARCH_PROXY=${NEW_PROXY:-${IMAGE_SEARCH_PROXY:-}}

# Cookies 按引擎配置，旧全局变量需要明确归属。
COOKIE_KEY=""
COOKIE_VALUE=""
read -r -p "Cookies 属于哪个引擎（如 Yandex / Bing；留空=不修改现有 cookies): " NEW_COOKIE_ENGINE
if [ -n "$NEW_COOKIE_ENGINE" ]; then
    COOKIE_KEY="IMAGE_SEARCH_COOKIES_$(printf '%s' "$NEW_COOKIE_ENGINE" | tr '[:lower:]-' '[:upper:]_')"
    read -r -p "请输入 ${NEW_COOKIE_ENGINE} 的 Cookies（形如 sid=xxx; other=yyy）: " NEW_COOKIES
    COOKIE_VALUE=${NEW_COOKIES:-}
    [ -z "$COOKIE_VALUE" ] && fail "选了引擎却没给 cookies 值；不想改就留空引擎名"
fi

# 只替换脚本管理的键，保留其他配置。
PRESERVED=""
if [ -f "$ENV_PATH" ]; then
    PRESERVED=$(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ENV_PATH" \
        | grep -vE '^(HOST|PORT|MCP_AUTH_TOKEN|IMAGE_SEARCH_API_KEY|IMAGE_SEARCH_PROXY)=' \
        | grep -vE "^${COOKIE_KEY:-IMAGE_SEARCH_COOKIES_NEVER}=" \
        || true)
fi

# 旧式的全局 cookies 若没有归属键，服务会拒绝启动。保留它是对的（不能偷偷删凭据），
# 但必须说出来，否则人只会看到"服务起不来"。
if printf '%s\n' "$PRESERVED" | grep -q '^IMAGE_SEARCH_COOKIES='; then
    echo "警告: 旧的全局 IMAGE_SEARCH_COOKIES 已原样保留；它需要 IMAGE_SEARCH_COOKIES_ENGINE" >&2
    echo "      指明归属，否则服务会拒绝启动 —— 建议改用引擎专属的 IMAGE_SEARCH_COOKIES_<ENGINE>。" >&2
fi

{
    echo "# 基础运行配置"
    echo "HOST=0.0.0.0"
    echo "PORT=$(env_quote "$PORT")"
    echo "MCP_AUTH_TOKEN=$(env_quote "$MCP_AUTH_TOKEN")"
    echo
    echo "# 搜图引擎可选配置"
    echo "IMAGE_SEARCH_API_KEY=$(env_quote "$IMAGE_SEARCH_API_KEY")"
    echo "IMAGE_SEARCH_PROXY=$(env_quote "$IMAGE_SEARCH_PROXY")"
    if [ -n "$COOKIE_KEY" ]; then
        echo "${COOKIE_KEY}=$(env_quote "$COOKIE_VALUE")"
    fi
    if [ -n "$PRESERVED" ]; then
        echo
        echo "# 以下键由部署脚本原样保留（各引擎 cookies / 自定义键）"
        printf '%s\n' "$PRESERVED"
    fi
} > "${ENV_PATH}.new" || fail "写 ${ENV_PATH}.new 失败"

mv "${ENV_PATH}.new" "$ENV_PATH" || fail "替换 ${ENV_PATH} 失败"
chmod 600 "$ENV_PATH"
echo -e "\n配置已保存至 .env（非托管键已保留）。"

# ---- 安装基础工具 ----
echo -e "\n[3/6] 安装基础工具 (curl)..."
sudo apt update || fail "apt update 失败"
sudo apt install -y curl ca-certificates || fail "apt install 失败"

# ---- 安装 uv 与 Python 3.12 ----
echo -e "\n[4/6] 安装 uv 并自动配置 Python 3.12..."
curl -LsSf https://astral.sh/uv/install.sh | sh || fail "uv 安装脚本失败"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
UV_BIN=$(find_uv)
[ -n "$UV_BIN" ] || fail "无法找到 uv 命令，请检查安装是否成功"
echo "使用 uv 路径: $UV_BIN"
"$UV_BIN" python install 3.12 || fail "uv python install 3.12 失败"

# ---- systemd 单元 ----
echo -e "\n[5/6] 创建 Systemd 服务..."
CURRENT_USER=$(whoami)
CURRENT_DIR=$(pwd)

WRITE_UNIT=1
if sudo test -f "${UNIT_PATH}"; then
    if [ "$REPLACE_UNIT" != "1" ]; then
        WRITE_UNIT=0
        echo "已存在 ${UNIT_PATH}，不覆盖。"
        echo "  .env 已更新，请核对现有单元的 EnvironmentFile 是否指向 ${ENV_PATH}。"
        echo "  若凭据写在单元的 Environment 中，需要单独更新该配置。"
        echo "  确实要替换单元就加 --replace-unit（会先按时间戳备份）。"
    else
        UNIT_BACKUP="${UNIT_PATH}.bak.$(date +%Y%m%d%H%M%S)"
        sudo cp -a "${UNIT_PATH}" "${UNIT_BACKUP}" || fail "备份单元失败"
        echo "已备份原单元 → ${UNIT_BACKUP}"
    fi
fi

if [ "$WRITE_UNIT" = "1" ]; then
    UNIT_TEMP=$(mktemp) || fail "无法创建临时单元文件"
    trap 'rm -f "$UNIT_TEMP"' EXIT
    unit_quote() { env_quote "${1//%/%%}"; }
    cat > "$UNIT_TEMP" <<EOF
[Unit]
Description=Image Search MCP Server
After=network.target

[Service]
User=$CURRENT_USER
WorkingDirectory=$(unit_quote "$CURRENT_DIR")
EnvironmentFile=$(unit_quote "$ENV_PATH")
# 使用 uv 运行托管的工具，并强制指定 Python 3.12
ExecStart=$(unit_quote "$UV_BIN") tool run --python 3.12 image-search-mcp --sse --host 0.0.0.0 --port ${PORT}
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
    sudo install -m 644 "$UNIT_TEMP" "$UNIT_PATH" || fail "写单元文件失败"
fi

# ---- 启动与校验 ----
echo -e "\n[6/6] 启动并激活服务..."
sudo systemctl daemon-reload || fail "daemon-reload 失败"
sudo systemctl enable "${UNIT_NAME}" || fail "enable ${UNIT_NAME} 失败"
sudo systemctl restart "${UNIT_NAME}" || fail "restart ${UNIT_NAME} 失败"

sleep 2
verify_service || exit 1

echo "------------------------------------------------"
echo "✅ 部署完成！"
echo "访问地址: http://$(curl -s --max-time 5 ifconfig.me || echo '<查不到出口IP>'):${PORT}/sse"
echo "Python版本: $("$UV_BIN" run --python 3.12 python --version 2>&1 || echo '<未知>')"
echo "------------------------------------------------"
echo "查看实时日志命令: journalctl -u ${UNIT_NAME} -f"
