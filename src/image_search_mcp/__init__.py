from importlib.metadata import PackageNotFoundError, version

from .server import mcp

try:
    __version__ = version("image-search-mcp")
except PackageNotFoundError:  # pragma: no cover - 未安装（直接跑源码）
    __version__ = "0.0.0+unknown"

__all__ = ["mcp", "__version__"]
