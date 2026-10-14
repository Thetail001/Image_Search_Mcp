"""本机 stdio 模式的受控本地图片读取：路径、类型与限制门禁。"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from image_bytes import jpeg_bytes, png_bytes, webp_bytes
from image_search_mcp import local_input

ENABLE_ENV = local_input.ENABLE_VAR
ROOTS_ENV = local_input.ROOTS_VAR


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------

@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "images"
    directory.mkdir()
    os.chmod(directory, 0o700)
    monkeypatch.setenv(ENABLE_ENV, "1")
    monkeypatch.setenv(ROOTS_ENV, str(directory))
    return directory


def _write(directory: Path, name: str, data: bytes) -> Path:
    target = directory / name
    target.write_bytes(data)
    os.chmod(target, 0o600)
    return target


def _code(exc_info: pytest.ExceptionInfo) -> str:
    return exc_info.value.code


# --------------------------------------------------------------------------
# 合法输入
# --------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [png_bytes(), jpeg_bytes(), webp_bytes()])
def test_supported_formats_are_read_byte_for_byte(root: Path, payload: bytes):
    path = _write(root, "image.bin", payload)
    assert local_input.read_image(str(path)) == payload


def test_read_image_does_not_require_the_extension(root: Path):
    path = _write(root, "no-extension", png_bytes())
    assert local_input.read_image(str(path)) == path.read_bytes()


# --------------------------------------------------------------------------
# 路径边界
# --------------------------------------------------------------------------

def test_path_outside_configured_roots_is_denied(root: Path, tmp_path: Path):
    outside = tmp_path / "outside.png"
    outside.write_bytes(png_bytes())
    with pytest.raises(local_input.LocalInputError) as excinfo:
        local_input.read_image(str(outside))
    assert _code(excinfo) == "INPUT_PATH_DENIED"


def test_symlink_to_outside_is_denied(root: Path, tmp_path: Path):
    outside = tmp_path / "outside.png"
    outside.write_bytes(png_bytes())
    link = root / "link.png"
    link.symlink_to(outside)
    with pytest.raises(local_input.LocalInputError) as excinfo:
        local_input.read_image(str(link))
    assert _code(excinfo) == "INPUT_PATH_DENIED"


def test_symlinked_directory_component_is_denied(root: Path, tmp_path: Path):
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    _write(outside_dir, "a.png", png_bytes())
    (root / "sub").symlink_to(outside_dir, target_is_directory=True)
    with pytest.raises(local_input.LocalInputError) as excinfo:
        local_input.read_image(str(root / "sub" / "a.png"))
    assert _code(excinfo) == "INPUT_PATH_DENIED"


def test_symlink_component_check_is_a_real_gate(root: Path, tmp_path: Path):
    """第一道（逐段 lstat）自己也要能拦；否则它只是装饰。"""
    link = root / "link-dir"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(local_input.LocalInputError) as excinfo:
        local_input._assert_no_symlink_components(link, code="INPUT_PATH_DENIED")
    assert _code(excinfo) == "INPUT_PATH_DENIED"


def test_open_beneath_refuses_symlink_components(root: Path, tmp_path: Path):
    """第二道（逐段 openat + O_NOFOLLOW）不能靠第一道活着。"""
    outside_dir = tmp_path / "outside-open"
    outside_dir.mkdir()
    (outside_dir / "a.png").write_bytes(png_bytes())
    (root / "sub").symlink_to(outside_dir, target_is_directory=True)
    with pytest.raises(local_input.LocalInputError) as excinfo:
        local_input._open_beneath(root, root / "sub" / "a.png")
    assert _code(excinfo) == "INPUT_PATH_DENIED"


def test_parent_traversal_is_rejected(root: Path, tmp_path: Path):
    outside = tmp_path / "outside.png"
    outside.write_bytes(png_bytes())
    with pytest.raises(local_input.LocalInputError) as excinfo:
        local_input.read_image(str(root / ".." / "outside.png"))
    assert _code(excinfo) in {"INPUT_PATH_INVALID", "INPUT_PATH_DENIED"}


@pytest.mark.parametrize("raw", [
    "relative.png",
    "./relative.png",
    "~/a.png",
    "file:///etc/passwd",
    "https://example.com/a.jpg",
    "data:image/png;base64,AAAA",
    "",
    "   ",
])
def test_non_absolute_paths_are_rejected(root: Path, raw: str):
    with pytest.raises(local_input.LocalInputError) as excinfo:
        local_input.read_image(raw)
    assert _code(excinfo) == "INPUT_PATH_INVALID"


def test_control_characters_in_path_are_rejected(root: Path):
    with pytest.raises(local_input.LocalInputError) as excinfo:
        local_input.read_image(str(root / "a\n.png"))
    assert _code(excinfo) == "INPUT_PATH_INVALID"


def test_error_text_does_not_echo_the_path(root: Path, tmp_path: Path):
    outside = str(tmp_path / "secret-name.png")
    with pytest.raises(local_input.LocalInputError) as excinfo:
        local_input.read_image(outside)
    assert "secret-name" not in str(excinfo.value)


# --------------------------------------------------------------------------
# 文件类型与内容
# --------------------------------------------------------------------------

def test_directory_is_rejected(root: Path):
    with pytest.raises(local_input.LocalInputError) as excinfo:
        local_input.read_image(str(root))
    assert _code(excinfo) == "INPUT_FILE_UNAVAILABLE"


def test_fifo_is_rejected_without_blocking(root: Path):
    fifo = root / "pipe"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(local_input.LocalInputError) as excinfo:
        local_input.read_image(str(fifo))
    assert _code(excinfo) == "INPUT_FILE_UNAVAILABLE"


def test_missing_file_is_unavailable(root: Path):
    with pytest.raises(local_input.LocalInputError) as excinfo:
        local_input.read_image(str(root / "missing.png"))
    assert _code(excinfo) == "INPUT_FILE_UNAVAILABLE"


def test_empty_file_is_not_an_image(root: Path):
    path = _write(root, "empty.png", b"")
    with pytest.raises(local_input.LocalInputError) as excinfo:
        local_input.read_image(str(path))
    assert _code(excinfo) == "INPUT_IMAGE_INVALID"


def test_unknown_format_is_rejected(root: Path):
    path = _write(root, "notes.txt", b"just text, not an image")
    with pytest.raises(local_input.LocalInputError) as excinfo:
        local_input.read_image(str(path))
    assert _code(excinfo) == "INPUT_FILE_TYPE_UNSUPPORTED"


def test_svg_and_pdf_are_rejected(root: Path):
    svg = _write(root, "a.svg", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>")
    pdf = _write(root, "a.pdf", b"%PDF-1.7\n")
    for path in (svg, pdf):
        with pytest.raises(local_input.LocalInputError) as excinfo:
            local_input.read_image(str(path))
        assert _code(excinfo) == "INPUT_FILE_TYPE_UNSUPPORTED"


def test_truncated_jpeg_is_rejected(root: Path):
    path = _write(root, "broken.jpg", b"\xff\xd8\xff\x00\x00\x00")
    with pytest.raises(local_input.LocalInputError) as excinfo:
        local_input.read_image(str(path))
    assert _code(excinfo) == "INPUT_IMAGE_INVALID"


def test_animated_png_is_rejected(root: Path):
    path = _write(root, "anim.png", png_bytes(animated=True))
    with pytest.raises(local_input.LocalInputError) as excinfo:
        local_input.read_image(str(path))
    assert _code(excinfo) == "INPUT_IMAGE_INVALID"


def test_animated_webp_is_rejected(root: Path):
    path = _write(root, "anim.webp", webp_bytes(animated=True))
    with pytest.raises(local_input.LocalInputError) as excinfo:
        local_input.read_image(str(path))
    assert _code(excinfo) == "INPUT_IMAGE_INVALID"


# --------------------------------------------------------------------------
# 限制
# --------------------------------------------------------------------------

def test_file_larger_than_budget_is_rejected(root: Path):
    path = _write(root, "big.png", png_bytes())
    os.truncate(path, local_input.MAX_BYTES + 1)
    with pytest.raises(local_input.LocalInputError) as excinfo:
        local_input.read_image(str(path))
    assert _code(excinfo) == "INPUT_FILE_TOO_LARGE"


def test_pixel_budget_is_enforced(root: Path):
    path = _write(root, "huge.png", png_bytes(6000, 6000))
    assert 6000 * 6000 > local_input.MAX_PIXELS
    with pytest.raises(local_input.LocalInputError) as excinfo:
        local_input.read_image(str(path))
    assert _code(excinfo) == "INPUT_IMAGE_LIMIT_EXCEEDED"


def test_edge_limit_is_enforced(root: Path):
    path = _write(root, "wide.png", png_bytes(local_input.MAX_EDGE + 1, 1))
    with pytest.raises(local_input.LocalInputError) as excinfo:
        local_input.read_image(str(path))
    assert _code(excinfo) == "INPUT_IMAGE_LIMIT_EXCEEDED"


# --------------------------------------------------------------------------
# 启用与 allowlist 配置
# --------------------------------------------------------------------------

def test_disabled_without_the_enable_switch(root: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(ENABLE_ENV, raising=False)
    assert local_input.enabled() is False
    with pytest.raises(local_input.LocalInputError) as excinfo:
        local_input.read_image(str(_write(root, "a.png", png_bytes())))
    assert _code(excinfo) == "LOCAL_INPUT_DISABLED"


def test_enable_without_roots_is_a_configuration_error(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ENABLE_ENV, "1")
    monkeypatch.delenv(ROOTS_ENV, raising=False)
    assert local_input.enable_requested() is True
    assert local_input.enabled() is False
    with pytest.raises(local_input.LocalInputError) as excinfo:
        local_input.validate_roots()
    assert _code(excinfo) == "LOCAL_INPUT_DISABLED"


def test_enable_switch_accepts_only_explicit_truthy_values(
    root: Path, monkeypatch: pytest.MonkeyPatch
):
    for raw, expected in (("1", True), ("true", True), ("YES", True), ("0", False), ("", False)):
        monkeypatch.setenv(ENABLE_ENV, raw)
        assert local_input.enabled() is expected, raw


def test_relative_root_is_rejected(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ENABLE_ENV, "1")
    monkeypatch.setenv(ROOTS_ENV, "relative/dir")
    with pytest.raises(local_input.LocalInputError):
        local_input.validate_roots()


@pytest.mark.parametrize("forbidden", ["/", "/root", "/tmp", "/etc"])
def test_system_roots_are_rejected(monkeypatch: pytest.MonkeyPatch, forbidden: str):
    monkeypatch.setenv(ENABLE_ENV, "1")
    monkeypatch.setenv(ROOTS_ENV, forbidden)
    assert local_input.enabled() is False
    with pytest.raises(local_input.LocalInputError):
        local_input.validate_roots()


def test_symlinked_root_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link-root"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv(ENABLE_ENV, "1")
    monkeypatch.setenv(ROOTS_ENV, str(link))
    with pytest.raises(local_input.LocalInputError):
        local_input.validate_roots()


def test_group_or_world_writable_root_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    directory = tmp_path / "loose"
    directory.mkdir()
    os.chmod(directory, 0o777)
    monkeypatch.setenv(ENABLE_ENV, "1")
    monkeypatch.setenv(ROOTS_ENV, str(directory))
    assert directory.stat().st_mode & stat.S_IWOTH
    with pytest.raises(local_input.LocalInputError):
        local_input.validate_roots()


def test_multiple_roots_are_supported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    first = tmp_path / "a"
    second = tmp_path / "b"
    for directory in (first, second):
        directory.mkdir()
        os.chmod(directory, 0o700)
    monkeypatch.setenv(ENABLE_ENV, "1")
    monkeypatch.setenv(ROOTS_ENV, os.pathsep.join([str(first), str(second)]))
    roots = local_input.validate_roots()
    assert {str(root) for root in roots} == {str(first), str(second)}
    assert local_input.read_image(str(_write(second, "a.png", png_bytes())))
