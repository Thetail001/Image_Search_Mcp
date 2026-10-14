#!/usr/bin/env bash
# 运行单元测试、检查脚本自检和反向测试，任一步失败时非零退出。
#
#   ./check.sh                 # 完整检查
#   ./check.sh --fast          # 跳过反向测试
#   ./check.sh --reverse-only  # 只运行反向测试
#
# 每一步显式记录退出码，以兼容 git check-ignore 等工具的正常非零返回值。

set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")" || exit 2

FAST=0
REVERSE_ONLY=0
usage() {
  cat <<'USAGE'
用法：./check.sh [--fast | --reverse-only]

  无选项          运行完整检查
  --fast          跳过反向测试
  --reverse-only  只运行反向测试
  -h, --help      显示说明
USAGE
}
for arg in "$@"; do
  case "$arg" in
    --fast) FAST=1 ;;
    --reverse-only) REVERSE_ONLY=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数：$arg" >&2; exit 2 ;;
  esac
done

if [[ $FAST -eq 1 && $REVERSE_ONLY -eq 1 ]]; then
  echo "--fast 与 --reverse-only 不能同时使用" >&2
  exit 2
fi

# ---------------------------------------------------------------------------
# 解释器
# ---------------------------------------------------------------------------

# 使用绝对解释器路径，确保临时副本内的子进程可调用同一环境。
if [[ -x .venv/bin/python ]]; then
  PY="$(pwd)/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
else
  echo "找不到 Python 解释器" >&2
  exit 2
fi

FAILED_STEPS=()
PASSED_STEPS=()

step() {  # step <名字> <命令...>
  local name="$1"; shift
  echo
  echo "── $name"
  echo "   \$ $*"
  # 立即保存命令退出码，避免读取到 if 复合命令的状态。
  local code=0
  "$@" || code=$?
  if [[ $code -eq 0 ]]; then
    PASSED_STEPS+=("$name")
    echo "   → 通过"
    return 0
  fi
  FAILED_STEPS+=("$name（退出码 $code）")
  echo "   → 不通过（退出码 $code）"
  return "$code"
}

echo "仓库：$(pwd)"
echo "解释器：$PY ($("$PY" -V 2>&1))"
echo "提交：$(git rev-parse --short HEAD 2>/dev/null || echo '（无 git）')" \
     "$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"

# ---------------------------------------------------------------------------
# 1. 代码能被导入（最快的一道，捕获语法错误）
# ---------------------------------------------------------------------------

if [[ $REVERSE_ONLY -eq 0 ]]; then

  step "模块可导入" "$PY" -c "import image_search_mcp, image_search_mcp.main, image_search_mcp.safe_download, image_search_mcp.credentials, image_search_mcp.params, image_search_mcp.local_input; print('ok')"

  # -------------------------------------------------------------------------
  # 2. 单元测试
  # -------------------------------------------------------------------------

  step "单元测试" "$PY" -m pytest -q --timeout=30 --timeout-method=thread

  # -------------------------------------------------------------------------
  # 3. 门禁自检：没有测试源码被 git 忽略
  # -------------------------------------------------------------------------

  # 检查测试源码是否被 Git 忽略，同时验证检查脚本能发现故障。
  step "门禁自检：测试源码未被 git 忽略（含反向自检）" \
      "$PY" tools/gate_tests_not_ignored.py --self-test
  step "门禁自检：当前仓库无被忽略的测试源码" \
      "$PY" tools/gate_tests_not_ignored.py

  # -------------------------------------------------------------------------
  # 3b. 门禁自检：本脚本自己上报的退出码是真的
  # -------------------------------------------------------------------------

  # 验证 step() 上报的退出码与命令实际退出码一致。
  step "门禁自检：check.sh 上报的退出码是真的（含反向自检）" \
      "$PY" tools/gate_check_sh_exit_code.py

  # -------------------------------------------------------------------------
  # 3b-2. 门禁自检：check.sh 选出来的解释器是可用绝对路径
  # -------------------------------------------------------------------------

  # 验证有无 .venv 两种布局下的解释器选择。
  step "门禁自检：check.sh 的解释器是绝对路径（含反向自检）" \
      "$PY" tools/gate_check_sh_interpreter.py

  # -------------------------------------------------------------------------
  # 3c. 部署脚本动的是线上真正在跑的那个单元
  # -------------------------------------------------------------------------

  # 这类缺陷不会自己暴露：脚本每步都成功、退出码 0，但重启的不是真正在跑的服务。
  # 桩掉 sudo/systemctl 真跑脚本，断言它重启的单元名；工具内部带反向自检。
  step "门禁：部署脚本的单元名与线上一致（含反向自检）" \
      "$PY" tools/gate_deploy_script_unit.py

  # -------------------------------------------------------------------------
  # 3d. 文档里的状态段是刚生成的，不是过期数字
  # -------------------------------------------------------------------------

  # 校验文档中的测试和故障注入统计。
  step "门禁：文档状态段与产物一致（含反向自检）" \
      "$PY" tools/gate_status_block_fresh.py --self-test

fi

# ---------------------------------------------------------------------------
# 4. 反向测试：证明门禁会红
# ---------------------------------------------------------------------------

if [[ $FAST -eq 0 ]]; then
  # PY 本身已经是绝对路径（见文件顶部），直接传即可 —— 不要再拼 $(pwd)：
  # 没有 .venv 的布局下那会拼出一个不存在的路径（CI 就是这种布局）。
  step "反向测试（把缺陷放回去，确认门禁会红）" \
      "$PY" tools/reverse_tests/run.py --python "$PY" || true
fi

# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

echo
echo "=============================================================="
echo "通过 ${#PASSED_STEPS[@]} 步"
for name in "${PASSED_STEPS[@]:-}"; do
  [[ -n "$name" ]] && echo "  可以 $name"
done

if [[ ${#FAILED_STEPS[@]} -gt 0 ]]; then
  echo "不通过 ${#FAILED_STEPS[@]} 步："
  for name in "${FAILED_STEPS[@]}"; do
    echo "  不行 $name"
  done
  exit 1
fi

echo "全部通过。"
