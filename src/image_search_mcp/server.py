from typing import Optional, Dict, Any, List
import json
import base64
import traceback
import sys
import os

try:
    from fastmcp import FastMCP
except ImportError:
    from mcp.server.fastmcp import FastMCP

from PicImageSearch import (
    Network, SauceNAO, Google, TraceMoe, Ascii2D, BaiDu, Bing, 
    EHentai, GoogleLens, Iqdb, Tineye, Yandex
)

# Initialize FastMCP server
mcp = FastMCP("image-search")

# Engine mapping
ENGINES = {
    "SauceNAO": SauceNAO,
    "Google": Google,
    "TraceMoe": TraceMoe,
    "Ascii2D": Ascii2D,
    "BaiDu": BaiDu,
    "Bing": Bing,
    "EHentai": EHentai,
    "GoogleLens": GoogleLens,
    "Iqdb": Iqdb,
    "Tineye": Tineye,
    "Yandex": Yandex
}

# Engine Information (Briefs & Advanced Params)
ENGINE_INFO = {
    "Yandex": {
        "brief": "综合能力最强的通用搜图引擎，对裁剪、翻转和修改过的图片识别率极高。支持情况：[URL: 是, 文件: 是]。。",
        "params": {
            "rpt": "Search mode (default: 'imageview')",
            "cbir_page": "Page type (default: 'sites')"
        }
    },
    "SauceNAO": {
        "brief": "专注二次元插画、漫画和动漫截图搜索。Pixiv 图片识别率极高。支持情况：[URL: 是, 文件: 是]。需要 API Key。",
        "params": {
            "numres": "Max results (1-40, default: 5)",
            "hide": "Filter results (0:none, 1:explicit, 2:questionable, 3:safe, default: 0)",
            "minsim": "Minimum similarity % (0-100, default: 30)",
            "db": "Specific DB ID to search (default: 999 for all)",
            "output_type": "Output format (default: 2)",
            "testmode": "Test mode (default: 0)"
        }
    },
    "Ascii2D": {
        "brief": "专注二次元插画，特别适合查找 Twitter 和 Pixiv 上的原始画师。支持情况：[URL: 是, 文件: 是]。支持颜色搜索和特征搜索。",
        "params": {
            "bovw": "Boolean. If true, use 'Feature Search' (better for modified/cropped images). If false, use 'Color Search'. (Default: false)"
        }
    },
    "TraceMoe": {
        "brief": "动漫截图专用搜索引擎，可识别具体番剧名称、集数和时间点。支持情况：[URL: 是, 文件: 是]。。",
        "params": {
            "cutBorders": "Boolean. Cut black borders (default: true)"
        }
    },
    "EHentai": {
        "brief": "专门搜索 E-Hentai 和 ExHentai 的图库。支持情况：[URL: 是, 文件: 是]。搜索 ExHentai 需要配置 Cookies。",
        "params": {
            "is_ex": "Boolean. If true, search ExHentai (requires cookies). (Default: false)",
            "covers": "Boolean. Search only covers. (Default: false)",
            "similar": "Boolean. Enable similarity scanning. (Default: true)",
            "exp": "Boolean. Include expunged galleries. (Default: false)"
        }
    },
    "Google": {
        "brief": "谷歌通用搜图。适合寻找类似图片或图片来源。支持情况：[URL: 是, 文件: 是]。。",
        "params": {}
    },
    "GoogleLens": {
        "brief": "谷歌智慧镜头。擅长识别物体、文字和商品。支持情况：[URL: 是, 文件: 是]。。",
        "params": {}
    },
    "BaiDu": {
        "brief": "百度识图，国内资源识别较好。支持情况：[URL: 是, 文件: 是]。。",
        "params": {}
    },
    "Bing": {
        "brief": "必应视觉搜索。支持情况：[URL: 是, 文件: 是]。。",
        "params": {}
    },
    "Iqdb": {
        "brief": "多站聚合搜索 (Danbooru, Konachan, etc.)，适合二次元图片。支持情况：[URL: 是, 文件: 是]。。",
        "params": {}
    },
    "Tineye": {
        "brief": "老牌反向搜图引擎，擅长寻找精确匹配的图片来源。支持情况：[URL: 是, 文件: 是]。。",
        "params": {}
    }
}


def _parse_proxy(proxy: str) -> Dict[str, str]:
    """Helper to format proxy string for httpx."""
    if not proxy:
        return {}
    return {
        "http://": proxy,
        "https://": proxy
    }

def _parse_cookies(cookie_str: str) -> Dict[str, str]:
    """Helper to parse cookie string into dict."""
    if not cookie_str:
        return {}
    cookies = {}
    for item in cookie_str.split(";"):
        if "=" in item:
            k, v = item.strip().split("=", 1)
            cookies[k] = v
    return cookies

@mcp.tool()
def get_engine_info(engine_name: str = "all") -> str:
    """
    Get information about supported search engines.
    
    Args:
        engine_name: The name of the engine to get details for, or "all" for a summary list.
                     Default: "all".
    """
    if engine_name.lower() == "all":
        # Return summary list
        summary = ["Supported Search Engines:"]
        for name, info in ENGINE_INFO.items():
            summary.append(f"- {name}: {info['brief']}")
        return "\n".join(summary)
    
    # Return specific engine details
    target_name = None
    for name in ENGINE_INFO.keys():
        if name.lower() == engine_name.lower():
            target_name = name
            break
    
    if not target_name:
        return f"Error: Engine '{engine_name}' not found. Supported: {', '.join(ENGINES.keys())}"
    
    info = ENGINE_INFO[target_name]
    details = [f"=== {target_name} ==="]
    details.append(f"Brief: {info['brief']}")
    
    if info['params']:
        details.append("\nAdvanced Parameters (pass via 'extra_params_json'):")
        for param, desc in info['params'].items():
            details.append(f"- {param}: {desc}")
    else:
        details.append("\nNo specific advanced parameters.")
        
    return "\n".join(details)


def _format_result_item(item: Any, engine: str) -> str:
    """Helper to format a single result item based on engine type."""
    lines = []
    
    # Universal attributes (if available)
    if hasattr(item, "title") and item.title:
        lines.append(f"Title: {item.title}")
    if hasattr(item, "url") and item.url:
        lines.append(f"URL: {item.url}")
    if hasattr(item, "thumbnail") and item.thumbnail:
        lines.append(f"Thumbnail: {item.thumbnail}")
        
    # Engine specific attributes
    if engine == "Iqdb":
        if hasattr(item, "similarity") and item.similarity is not None:
            lines.append(f"Similarity: {item.similarity}%")

    if engine == "SauceNAO":
        if hasattr(item, "author") and item.author:
            lines.append(f"Author: {item.author}")
        if hasattr(item, "pixiv_id") and item.pixiv_id:
            lines.append(f"Pixiv ID: {item.pixiv_id}")
        if hasattr(item, "member_id") and item.member_id:
            lines.append(f"Member ID: {item.member_id}")
            
    elif engine == "TraceMoe":
        if hasattr(item, "episode") and item.episode:
            lines.append(f"Episode: {item.episode}")
        
        # Time (TraceMoeItem uses .From and .To)
        start_time = getattr(item, "From", None)
        end_time = getattr(item, "To", None)
        if start_time is not None:
             lines.append(f"Time: {start_time}s - {end_time if end_time else '?'}s")
        
        # Titles (TraceMoeItem has multiple title fields)
        if hasattr(item, "title_english") and item.title_english:
             lines.append(f"English Title: {item.title_english}")
        if hasattr(item, "title_romaji") and item.title_romaji:
             lines.append(f"Romaji Title: {item.title_romaji}")
        if hasattr(item, "title_native") and item.title_native:
             lines.append(f"Native Title: {item.title_native}")
        if hasattr(item, "filename") and item.filename:
             lines.append(f"Filename: {item.filename}")
             
    elif engine == "Ascii2D":
         if hasattr(item, "author") and item.author:
            lines.append(f"Author: {item.author}")
         if hasattr(item, "author_url") and item.author_url:
            lines.append(f"Author URL: {item.author_url}")
         if hasattr(item, "source") and item.source:
             lines.append(f"Source Type: {item.source}")
             
    elif engine == "EHentai":
        if hasattr(item, "type") and item.type:
            lines.append(f"Category: {item.type}")
        if hasattr(item, "date") and item.date:
            lines.append(f"Date: {item.date}")
            
    elif engine == "Yandex":
        if hasattr(item, "source") and item.source:
            lines.append(f"Source: {item.source}")
        if hasattr(item, "content") and item.content:
            lines.append(f"Content: {item.content}")
        if hasattr(item, "size") and item.size:
            lines.append(f"Size: {item.size}")

    # Fallback for generic lists of URLs (like ext_urls)
    if hasattr(item, "ext_urls") and item.ext_urls:
        lines.append(f"External URLs: {item.ext_urls}")
        
    return "\n".join(lines)


async def _search_image_logic(
    source: str,
    engine: str = "Yandex", 
    extra_params_json: Optional[str] = None,
    limit: int = 5
) -> str:
    """Core logic for image search, separated for testing."""
    try:
        # Load config from environment variables
        api_key = os.environ.get("IMAGE_SEARCH_API_KEY")
        cookies = os.environ.get("IMAGE_SEARCH_COOKIES")
        proxy = os.environ.get("IMAGE_SEARCH_PROXY") or os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")

        # 1. Parse extra params
        extra_params = {}
        if extra_params_json:
            try:
                extra_params = json.loads(extra_params_json)
            except json.JSONDecodeError:
                return "Error: extra_params_json is not valid JSON."

        engine_cls = ENGINES.get(engine)
        if not engine_cls:
            return f"Error: Unsupported engine '{engine}'. Use get_engine_info('all') to see available options."
        
        # 2. Configure Network/Engine Args
        network_kwargs = {}
        if proxy:
            network_kwargs["proxies"] = _parse_proxy(proxy)
        
        cookie_dict = {}
        if cookies:
            cookie_dict = _parse_cookies(cookies)
            network_kwargs["cookies"] = cookie_dict

        # Engine-specific Init Params
        init_kwargs = {}
        if engine == "SauceNAO":
            if api_key:
                init_kwargs["api_key"] = api_key
            for k in ["numres", "hide", "minsim", "db", "output_type", "testmode"]:
                if k in extra_params:
                    init_kwargs[k] = extra_params.pop(k)
        
        elif engine == "EHentai":
            # EHentai specific params
            for k in ["is_ex", "covers", "similar", "exp"]:
                if k in extra_params:
                    init_kwargs[k] = extra_params.pop(k)
        
        elif engine == "Ascii2D":
            if "bovw" in extra_params:
                init_kwargs["bovw"] = extra_params.pop("bovw")

        # 3. Execute Search within Network context
        async with Network(**network_kwargs) as net:
            # Instantiate Engine with the network client
            client = engine_cls(client=net, **init_kwargs)

            # 4. Prepare Search Arguments
            search_kwargs = {}
            
            if source.strip().lower().startswith(("http://", "https://")):
                search_kwargs["url"] = source.strip()
            else:
                b64_str = source
                if "," in b64_str:
                    b64_str = b64_str.split(",")[1]
                try:
                    image_bytes = base64.b64decode(b64_str)
                    search_kwargs["file"] = image_bytes
                except Exception as e:
                    return f"Error decoding Base64 string: {str(e)}"
            
            search_kwargs.update(extra_params)

            # 5. Execute Search
            resp = await client.search(**search_kwargs)

        # 6. Format Result
        result_str = f"Search Engine: {engine}\n"
             
        if hasattr(resp, "raw") and resp.raw:
            shown_count = min(len(resp.raw), limit)
            result_str += f"Found {len(resp.raw)} results (showing top {shown_count}):\n"
            for i, item in enumerate(resp.raw[:limit]):
                result_str += f"\n--- Result {i+1} ---\n"
                result_str += _format_result_item(item, engine)
                result_str += "\n"
        else:
            result_str += f"No results found or raw data unavailable.\n"
            if engine in ["Yandex", "Google", "Bing", "GoogleLens", "Tineye"]:
                result_str += f"Hint: '{engine}' often requires 'IMAGE_SEARCH_COOKIES' (and sometimes a proxy) to bypass bot protection.\n"
            result_str += f"Response: {resp}"
            
        return result_str

    except json.JSONDecodeError:
        return f"Error: Failed to parse response from {engine}. This usually indicates bot protection (CAPTCHA) or an API change. Try setting 'IMAGE_SEARCH_COOKIES' in .env."
    except Exception as e:
        return f"An error occurred during search: {str(e)}\n{traceback.format_exc()}"

@mcp.tool()

async def search_image(

    source: str,

    engine: str = "Yandex", 

    extra_params_json: Optional[str] = None,

    limit: int = 5

) -> str:

    """

    Perform a reverse image search.

    

    Args:

        source: The image input. 

                IMPORTANT: 

                1. If you have a public image URL, ALWAYS use the URL.

                2. If you have image data, use Base64 encoded string.

                3. NEVER provide a local file path (e.g., /AstrBot/data/...), as the server cannot access your local files.

        engine: The search engine to use (default: "Yandex").

                Other supported engines: SauceNAO, Google, TraceMoe, Ascii2D, EHentai, etc.

                Use `get_engine_info` tool to see the full list and capabilities.

        extra_params_json: (Optional) JSON string for advanced engine parameters.

        limit: Max number of results to return (default: 5).

    """

    return await _search_image_logic(source, engine, extra_params_json, limit)
