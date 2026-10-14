"""本机 stdio 模式的受控本地图片读取。

远程 HTTP/SSE 模式永远不接受本地路径。本模块只在两件事同时成立时生效：

1. ``IMAGE_SEARCH_LOCAL_INPUT`` 显式开启；
2. ``IMAGE_SEARCH_LOCAL_ROOTS`` 给出至少一个绝对目录 allowlist。

``main()`` 负责拒绝「本地输入 + HTTP 一起开」的配置；这里只管读取本身：
路径必须在 allowlist 内、任一组件都不能是符号链接，且只能读普通文件。
读取上限、格式与像素限制见本模块常量。错误信息不回显路径。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Final

#: 显式开关。只有明确的真值才生效，避免 ``IMAGE_SEARCH_LOCAL_INPUT=0`` 被当成开启。
ENABLE_VAR = "IMAGE_SEARCH_LOCAL_INPUT"
#: allowlist 目录，多个用 ``os.pathsep`` 分隔。
ROOTS_VAR = "IMAGE_SEARCH_LOCAL_ROOTS"

#: 与远程下载预算一致：8 MiB。
MAX_BYTES: Final = 8 * 1024 * 1024
#: 20 MP；超过这个量级的解码内存与时间都不可控。
MAX_PIXELS: Final = 20_000_000
#: 单边上限，挡掉 20000×1 这种绕过像素预算的畸形图。
MAX_EDGE: Final = 16384

_TRUTHY: Final = frozenset({"1", "true", "yes", "on"})

#: 这些目录本身不能当 allowlist root；写它们等于把整台机器交出去。
_FORBIDDEN_ROOTS: Final = frozenset({
    "/", "/root", "/tmp", "/home", "/etc", "/usr", "/var", "/opt", "/srv", "/proc", "/sys",
})

_PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"

#: JPEG 的 SOF 标记（帧头，带尺寸）。
_SOF_MARKERS: Final = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


class LocalInputError(ValueError):
    """本地图片输入被拒绝。``code`` 是给调用方看的稳定前缀。"""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"[{code}] {message}")


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

def enable_requested() -> bool:
    raw = (os.environ.get(ENABLE_VAR) or "").strip().lower()
    return raw in _TRUTHY


def configured_roots() -> tuple[Path, ...]:
    raw = os.environ.get(ROOTS_VAR) or ""
    return tuple(Path(part) for part in raw.split(os.pathsep) if part.strip())


def _assert_no_symlink_components(
    path: Path, *, code: str, missing_code: str | None = None
) -> None:
    """逐段 lstat，任何一段是符号链接就拒绝。

    只查 ``realpath`` 不够：realpath 之后的普通 open 与检查之间还有 TOCTOU 窗口，
    而且符号链接会让人以为文件在 allowlist 里。
    """
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError as exc:
            raise LocalInputError(missing_code or code, "路径不存在或不可读。") from exc
        except OSError as exc:
            raise LocalInputError(code, "路径中包含无法读取的目录组件。") from exc
        if stat.S_ISLNK(info.st_mode):
            raise LocalInputError(code, "路径中包含符号链接，已拒绝。")


def validate_roots() -> tuple[Path, ...]:
    """校验并返回真实路径形式的 allowlist；未开启时返回空元组。"""
    if not enable_requested():
        return ()

    roots = configured_roots()
    if not roots:
        raise LocalInputError(
            "LOCAL_INPUT_DISABLED",
            f"{ENABLE_VAR} 已开启，但 {ROOTS_VAR} 为空；本机图片输入需要至少一个目录 allowlist。",
        )

    resolved: list[Path] = []
    for root in roots:
        if not root.is_absolute():
            raise LocalInputError(
                "LOCAL_INPUT_DISABLED", f"{ROOTS_VAR} 里的目录必须是绝对路径。"
            )
        _assert_no_symlink_components(root, code="LOCAL_INPUT_DISABLED")
        try:
            info = os.stat(root)
        except OSError as exc:
            raise LocalInputError(
                "LOCAL_INPUT_DISABLED", f"{ROOTS_VAR} 里的目录不存在或不可读。"
            ) from exc
        if not stat.S_ISDIR(info.st_mode):
            raise LocalInputError(
                "LOCAL_INPUT_DISABLED", f"{ROOTS_VAR} 里必须是目录。"
            )
        real = Path(os.path.realpath(root))
        if str(real) in _FORBIDDEN_ROOTS:
            raise LocalInputError(
                "LOCAL_INPUT_DISABLED",
                "allowlist 不允许指向系统目录；请使用专门的附件 cache 或 staging 子目录。",
            )
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise LocalInputError(
                "LOCAL_INPUT_DISABLED",
                "allowlist 目录不能对同组或其他用户可写。",
            )
        resolved.append(real)
    return tuple(resolved)


def enabled() -> bool:
    """本机图片输入是否可用：显式开启 + allowlist 有效。"""
    if not enable_requested():
        return False
    try:
        return bool(validate_roots())
    except LocalInputError:
        return False


# --------------------------------------------------------------------------
# 路径与读取
# --------------------------------------------------------------------------

def _validate_path_shape(raw: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise LocalInputError("INPUT_PATH_INVALID", "path 需要一个非空的绝对本地路径。")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise LocalInputError("INPUT_PATH_INVALID", "path 含控制字符。")
    if raw.startswith("~"):
        raise LocalInputError("INPUT_PATH_INVALID", "path 不接受 ~ 展开，请给绝对路径。")
    if "://" in raw or raw.lower().startswith("file:"):
        raise LocalInputError("INPUT_PATH_INVALID", "path 只接受本机绝对路径，不接受 URI。")

    path = Path(raw)
    if not path.is_absolute():
        raise LocalInputError("INPUT_PATH_INVALID", "path 必须是绝对路径。")
    if any(part == ".." for part in path.parts):
        raise LocalInputError("INPUT_PATH_INVALID", "path 不接受 .. 组件。")
    return path


def _containing_root(candidate: Path, roots: tuple[Path, ...]) -> Path:
    for root in roots:
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        return root
    raise LocalInputError("INPUT_PATH_DENIED", "path 不在允许读取的图片目录内。")


def _enforce_size_limit(size: int) -> None:
    if size > MAX_BYTES:
        raise LocalInputError("INPUT_FILE_TOO_LARGE", f"图片超过 {MAX_BYTES} 字节上限。")


def _open_beneath(root: Path, candidate: Path) -> int:
    """从 root 的目录 fd 出发逐段打开，不跟随任何符号链接。

    逐段 ``openat`` 而不是先 realpath 再普通 open：后者在检查与 open 之间有
    TOCTOU 窗口，可以用竞态把读取引到 allowlist 之外。
    """
    relative = candidate.relative_to(root)
    parts = [part for part in relative.parts if part not in ("", ".")]
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(root, directory_flags)
    try:
        for index, part in enumerate(parts):
            if part == "..":
                raise LocalInputError("INPUT_PATH_INVALID", "path 不接受 .. 组件。")
            last = index == len(parts) - 1
            flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            if not last:
                flags |= os.O_DIRECTORY
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except FileNotFoundError as exc:
        os.close(fd)
        raise LocalInputError("INPUT_FILE_UNAVAILABLE", "图片文件不存在或不可读。") from exc
    except OSError as exc:
        os.close(fd)
        code = "INPUT_PATH_DENIED" if exc.errno in {
            getattr(os, "ELOOP", 40), getattr(os, "ENOTDIR", 20),
        } else "INPUT_FILE_UNAVAILABLE"
        raise LocalInputError(code, "图片文件不存在或不可读。") from exc
    except BaseException:
        os.close(fd)
        raise


def _read_regular_file(root: Path, candidate: Path) -> bytes:
    fd = _open_beneath(root, candidate)

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise LocalInputError("INPUT_FILE_UNAVAILABLE", "只接受普通文件。")
        _enforce_size_limit(info.st_size)
        chunks: list[bytes] = []
        remaining = MAX_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)

    data = b"".join(chunks)
    # 读的时候文件可能长大了；按超限处理，不截断后猜。
    _enforce_size_limit(len(data))
    return data


# --------------------------------------------------------------------------
# 图片头解析（不依赖 Pillow：只读尺寸与动画标记）
# --------------------------------------------------------------------------

def _png_info(data: bytes) -> tuple[int, int, bool] | None:
    if len(data) < 33 or data[12:16] != b"IHDR":
        return None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if width <= 0 or height <= 0:
        return None
    animated = False
    index = 8
    while index + 8 <= len(data):
        length = int.from_bytes(data[index:index + 4], "big")
        ctype = data[index + 4:index + 8]
        if ctype == b"acTL":
            animated = True
            break
        if ctype in (b"IDAT", b"IEND"):
            break
        nxt = index + 12 + length
        if nxt <= index or nxt > len(data):
            break
        index = nxt
    return width, height, animated


def _jpeg_info(data: bytes) -> tuple[int, int, bool] | None:
    index = 2
    while index + 3 < len(data):
        if data[index] != 0xFF:
            return None
        marker = data[index + 1]
        if marker == 0xFF:
            index += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        if marker == 0xDA:  # SOS 还没见到 SOF：畸形或不受支持
            return None
        if index + 4 > len(data):
            return None
        segment_length = int.from_bytes(data[index + 2:index + 4], "big")
        if segment_length < 2 or index + 2 + segment_length > len(data):
            return None
        if marker in _SOF_MARKERS:
            if index + 9 > len(data):
                return None
            height = int.from_bytes(data[index + 5:index + 7], "big")
            width = int.from_bytes(data[index + 7:index + 9], "big")
            if width <= 0 or height <= 0:
                return None
            return width, height, False
        index += 2 + segment_length
    return None


def _webp_animated(data: bytes) -> bool:
    """扫 RIFF 子块，发现 ANIM 即视为动图。"""
    index = 12
    while index + 8 <= len(data):
        ctype = data[index:index + 4]
        length = int.from_bytes(data[index + 4:index + 8], "little")
        if ctype == b"ANIM":
            return True
        if ctype in (b"VP8 ", b"VP8L"):
            return False
        nxt = index + 8 + length + (length % 2)
        if nxt <= index or nxt > len(data):
            return False
        index = nxt
    return False


def _webp_info(data: bytes) -> tuple[int, int, bool] | None:
    if len(data) < 30 or data[8:12] != b"WEBP":
        return None
    ctype = data[12:16]
    offset = 20
    if ctype == b"VP8X":
        flags = data[offset]
        animated = bool(flags & 0x02)
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height, animated or _webp_animated(data)
    if ctype == b"VP8L":
        if data[offset] != 0x2F:
            return None
        bits = int.from_bytes(data[offset + 1:offset + 5], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height, _webp_animated(data)
    if ctype == b"VP8 ":
        if data[offset + 3:offset + 6] != b"\x9d\x01\x2a":
            return None
        width = int.from_bytes(data[offset + 6:offset + 8], "little") & 0x3FFF
        height = int.from_bytes(data[offset + 8:offset + 10], "little") & 0x3FFF
        if width <= 0 or height <= 0:
            return None
        return width, height, _webp_animated(data)
    return None


def _validate_image_bytes(data: bytes) -> None:
    if not data:
        raise LocalInputError("INPUT_IMAGE_INVALID", "图片文件为空。")

    if data.startswith(_PNG_SIGNATURE):
        info = _png_info(data)
    elif data.startswith(b"\xff\xd8\xff"):
        info = _jpeg_info(data)
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        info = _webp_info(data)
    else:
        raise LocalInputError(
            "INPUT_FILE_TYPE_UNSUPPORTED",
            "只接受 JPEG、PNG 和静态 WebP 图片。",
        )

    if info is None:
        raise LocalInputError("INPUT_IMAGE_INVALID", "图片头部损坏或不是受支持的格式。")

    width, height, animated = info
    if animated:
        raise LocalInputError("INPUT_IMAGE_INVALID", "动图不受支持，请提供静态图片。")
    if width <= 0 or height <= 0:
        raise LocalInputError("INPUT_IMAGE_INVALID", "图片尺寸无效。")
    if width * height > MAX_PIXELS or max(width, height) > MAX_EDGE:
        raise LocalInputError(
            "INPUT_IMAGE_LIMIT_EXCEEDED",
            f"图片尺寸超过上限（{MAX_PIXELS} 像素 / 单边 {MAX_EDGE}）。",
        )


def read_image(raw: str) -> bytes:
    """按 allowlist 安全读取一张本机图片，返回字节。"""
    if not enabled():
        raise LocalInputError(
            "LOCAL_INPUT_DISABLED",
            "本机图片输入未启用：这只在本机 stdio 模式且配置 allowlist 后可用。",
        )

    candidate = _validate_path_shape(raw)
    roots = validate_roots()
    root = _containing_root(candidate, roots)
    _assert_no_symlink_components(
        candidate, code="INPUT_PATH_DENIED", missing_code="INPUT_FILE_UNAVAILABLE"
    )

    data = _read_regular_file(root, candidate)
    _validate_image_bytes(data)
    return data
