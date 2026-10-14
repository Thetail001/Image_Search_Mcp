#!/usr/bin/env bash
# 构建 wheel 与 sdist，以 HEAD 提交时间归一化归档元数据。
# tools/build_release.sh          # 要求 Git 工作区干净
# tools/build_release.sh --check  # 重复构建并比较产物哈希
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
  # 归一化 tar 元数据和 gzip 时间戳，保持包内文件内容不变。
  python3 - "$SOURCE_DATE_EPOCH" <<'PY'
import copy
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
                info = copy.copy(member)
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = epoch
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
