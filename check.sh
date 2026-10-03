#!/usr/bin/env bash
#
# 一键验收。任何一步不过就非零退出。
#
#   ./check.sh                 # 全部（含反向测试，几分钟）
#   ./check.sh --fast          # 跳过反向测试，只跑单元测试与门禁自检
#   ./check.sh --reverse-only  # 只跑反向测试
#
# 这个脚本自己也是一道门禁，所以它有两个刻意的设计：
#
# 1. **不用 `set -e`**。`set -e` 会让"命令返回非零"和"检查不通过"混为一谈 ——
#    而 `git check-ignore` 在"没有匹配"时本来就返回 1，那是个正常结果。
#    每一步都显式判断退出码，顺手把"为什么红"打出来。
# 2. **反向测试是默认项，不是可选项**。一个永不失败的门禁比没有门禁更糟：
#    它看起来有人在看着。所以默认就要证明"注入缺陷时会红"。

set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")" || exit 2

FAST=0
REVERSE_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --fast) FAST=1 ;;
    --reverse-only) REVERSE_ONLY=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "未知参数：$arg" >&2; exit 2 ;;
  esac
done

# ---------------------------------------------------------------------------
# 解释器
# ---------------------------------------------------------------------------

if [[ -x .venv/bin/python ]]; then
  PY=.venv/bin/python
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
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
  if "$@"; then
    PASSED_STEPS+=("$name")
    echo "   → 通过"
    return 0
  fi
  local code=$?
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

  step "模块可导入" "$PY" -c "import image_search_mcp, image_search_mcp.main, image_search_mcp.safe_download, image_search_mcp.credentials, image_search_mcp.params; print('ok')"

  # -------------------------------------------------------------------------
  # 2. 单元测试
  # -------------------------------------------------------------------------

  step "单元测试" "$PY" -m pytest -q --timeout=30 --timeout-method=thread

  # -------------------------------------------------------------------------
  # 3. 门禁自检：没有测试源码被 git 忽略
  # -------------------------------------------------------------------------

  # 这道门禁自己带反向自检（工具内部造一个被忽略的测试文件，确认它会报警）。
  # 早期版本是在这里用一句 `git ls-files` 现写的，没有自检、而且写宽了 ——
  # 每次都被 tests/__pycache__/*.pyc 误报，那种门禁只会被关掉。
  step "门禁自检：测试源码未被 git 忽略（含反向自检）" \
      "$PY" tools/gate_tests_not_ignored.py --self-test
  step "门禁自检：当前仓库无被忽略的测试源码" \
      "$PY" tools/gate_tests_not_ignored.py

fi

# ---------------------------------------------------------------------------
# 4. 反向测试：证明门禁会红
# ---------------------------------------------------------------------------

if [[ $FAST -eq 0 ]]; then
  # 显式传绝对路径的解释器：反向测试会在临时副本里起子进程，
  # 相对路径在换过工作目录之后就不再有效。
  step "反向测试（把缺陷放回去，确认门禁会红）" \
      "$PY" tools/reverse_tests/run.py --python "$(pwd)/$PY" || true
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
