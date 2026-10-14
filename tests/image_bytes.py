"""受限图片读取测试用最小字节构造器（只保证头部可解析）。"""

from __future__ import annotations


def _chunk(ctype: bytes, payload: bytes) -> bytes:
    return len(payload).to_bytes(4, "big") + ctype + payload + b"\x00\x00\x00\x00"


def png_bytes(width: int = 2, height: int = 2, *, animated: bool = False) -> bytes:
    ihdr = (
        width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + bytes([8, 2, 0, 0, 0])
    )
    parts = [b"\x89PNG\r\n\x1a\n", _chunk(b"IHDR", ihdr)]
    if animated:
        parts.append(_chunk(b"acTL", (1).to_bytes(4, "big") + (0).to_bytes(4, "big")))
    parts.append(_chunk(b"IDAT", b"\x00"))
    parts.append(_chunk(b"IEND", b""))
    return b"".join(parts)


def jpeg_bytes(width: int = 2, height: int = 2) -> bytes:
    sof_payload = (
        bytes([8])
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + bytes([1, 0x11, 0, 3, 1, 0x22, 0, 2, 0x11, 0, 3, 0x11, 0])
    )
    sof = b"\xff\xc0" + (len(sof_payload) + 2).to_bytes(2, "big") + sof_payload
    app0 = b"\xff\xe0" + (16).to_bytes(2, "big") + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


def webp_bytes(width: int = 2, height: int = 2, *, animated: bool = False) -> bytes:
    flags = 0x02 if animated else 0x00
    payload = (
        bytes([flags])
        + b"\x00\x00\x00"
        + (width - 1).to_bytes(3, "little")
        + (height - 1).to_bytes(3, "little")
    )
    chunk = b"VP8X" + len(payload).to_bytes(4, "little") + payload
    body = b"WEBP" + chunk
    return b"RIFF" + len(body).to_bytes(4, "little") + body
