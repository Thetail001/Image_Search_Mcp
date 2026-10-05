#!/usr/bin/env bash
# 打发行产物（wheel + sdist），并打印哈希。
#
# 为什么要固定时间戳
# ------------------
# zip 与 gzip 会把**构建时刻**写进文件，于是"同一份源码"每次构建得到不同的 sha256。
# 实测踩过：23:30 构建的 wheel 是 bccaeac9…，00:10 重新构建同一个提交变成 252c6148…，
# 内容一样、字节不同 —— 结果就是"在真机上验过的那个文件"和"最后发出去的文件"
# 不是同一个。那正是"发完再测"的另一种形态。
#
# 固定成 HEAD 的提交时间之后，同一个提交重复构建得到同一个哈希，
# "测过的就是发布的"这句话才有可能被验证，而不是靠记忆。
#
# 用法
# ----
#     tools/build_release.sh          # 工作区必须干净
#     tools/build_release.sh --check  # 只检查产物是否与当前提交一致（重建后比哈希）
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -n "$(git status --porcelain)" ]]; then
  echo "工作区不干净 —— 先提交，否则产物无法从提交复现。" >&2
  git status --short >&2
  exit 2
fi

export SOURCE_DATE_EPOCH="$(git log -1 --format=%ct)"
head_short="$(git rev-parse --short HEAD)"
commit_time="$(git log -1 --format=%cI)"

build() {
  rm -rf dist
  uv build >/dev/null
  # 两级都不确定，都要处理：
  #
  # 1) gzip 头把"当前时刻"写进 MTIME（实测差的只有那 4 个字节）；
  # 2) 内层 tar 用的是 Python 默认的 pax 扩展头，里面带**亚秒** mtime ——
  #    生成文件（PKG-INFO / egg-info）的 mtime 带小数，每次构建都不同。
  #    实测：解开后文件内容、成员顺序、权限属主时间全一致，可内层 tar 哈希就是不同，
  #    差的全在 pax 头里。
  #
  # 所以：按 GNU tar 格式重建外层 tar（归一化 mtime/属主、丢掉 pax 头），
  # 再用固定 mtime 重新压。**包内文件内容一个字节不动。**
  python3 - "$SOURCE_DATE_EPOCH" <<'PY'
import gzip
import io
import pathlib
import sys
import tarfile

epoch = int(sys.argv[1])
for path in sorted(pathlib.Path("dist").glob("*.tar.gz")):
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(path.read_bytes())), mode="r:") as src:
        members = src.getmembers()
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as dst:
            for member in members:
                info = member.replace(uid=0, gid=0, uname="", gname="", mtime=epoch)
                info.pax_headers = {}
                payload = src.extractfile(member) if member.isfile() else None
                dst.addfile(info, payload)
    with path.open("wb") as handle:
        with gzip.GzipFile(filename="", mode="wb", fileobj=handle,
                           mtime=epoch, compresslevel=9) as gz:
            gz.write(buffer.getvalue())
    print(f"已归一化 {path.name}（tar 元数据 + gzip MTIME = {epoch}）")
PY
}

build
echo "SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH}  (${commit_time})"
echo "HEAD=${head_short}"
echo
sha256sum dist/*

if [[ "${1:-}" == "--check" ]]; then
  echo
  echo "---- 复现检查：用同一 epoch 再构建一次，哈希必须不变 ----"
  first="$(sha256sum dist/* | awk '{print $1, $2}' | sort)"
  build
  second="$(sha256sum dist/* | awk '{print $1, $2}' | sort)"
  if [[ "$first" == "$second" ]]; then
    echo "一致：同一提交重复构建得到相同哈希"
  else
    echo "不一致 —— 构建不可复现：" >&2
    diff <(echo "$first") <(echo "$second") >&2 || true
    exit 1
  fi
fi
