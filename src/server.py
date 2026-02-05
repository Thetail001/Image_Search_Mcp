from typing import Optional, Dict, Any
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
    SauceNAO, Google, TraceMoe, Ascii2D, BaiDu, Bing, 
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

def _parse_proxy(proxy: str) -> Dict[str, str]:
    """Helper to format proxy string for httpx."""
    if not proxy:
        return {}
    return {
        "http://": proxy,
        "https://": proxy
    }

def _get_engine_instance(engine_name: str, api_key: Optional[str], proxy: Optional[str], extra_params: Dict[str, Any]):
    """Factory to create engine instance with config."""
    engine_cls = ENGINES.get(engine_name)
    if not engine_cls:
        raise ValueError(f"Unsupported engine: {engine_name}. Available: {list(ENGINES.keys())}")
    
    # Common kwargs
    kwargs = {}
    if proxy:
        kwargs["proxies"] = _parse_proxy(proxy)
    
    # Add extra params to init kwargs (e.g. timeout)
    # Note: Some engines take specific init params like api_key
    
    # Engine specific handling
    if engine_name == "SauceNAO":
        if api_key:
            kwargs["api_key"] = api_key
        # Merge extra params that might belong to init (like numres, hide)
        # For simplicity, we pass extra_params to init if they match known args, 
        # but most PicImageSearch engines are flexible.
        # SauceNAO specific init args: numres, hide, minsim, etc.
        # We can perform a rough merge
        kwargs.update(extra_params)
        
    elif engine_name == "EHentai":
        # EHentai might need cookies or specific settings
        if "cookies" in extra_params:
            kwargs["cookies"] = extra_params["cookies"]
        kwargs.update(extra_params)
    
    else:
        # For other engines, pass extra params to init if applicable, 
        # mostly they go to search method, but let's allow flexibility.
        # However, we must be careful not to pass search-only params to init.
        # Since we can't easily distinguish without introspection, 
        # we will rely on users passing 'init_params' inside extra_params if needed,
        # or just pass them if safe.
        # A safer bet: pass proxy to init. Pass others to search.
        pass

    return engine_cls(**kwargs)

@mcp.tool()
def list_engines() -> str:
    """List all supported image search engines."""
    return f"Supported engines: {', '.join(ENGINES.keys())}"

async def _search_image_logic(
    engine: str, 
    source: str, 
    api_key: Optional[str] = None, 
    proxy: Optional[str] = None,
    extra_params_json: Optional[str] = None
) -> str:
    try:
        # 1. Parse extra params
        extra_params = {}
        if extra_params_json:
            try:
                extra_params = json.loads(extra_params_json)
            except json.JSONDecodeError:
                return "Error: extra_params_json is not valid JSON."

        # 2. Instantiate Engine
        # Note: We are passing extra_params to init for now if it's SauceNAO, 
        # but realistically most params are for search().
        # Let's separate them if possible or just pass proxy to init.
        
        engine_cls = ENGINES.get(engine)
        if not engine_cls:
            return f"Error: Unsupported engine '{engine}'. Use list_engines() to see available options."
        
        init_kwargs = {}
        if proxy:
            init_kwargs["proxies"] = _parse_proxy(proxy)
        
        if engine == "SauceNAO":
            if api_key:
                init_kwargs["api_key"] = api_key
            # SauceNAO specific init params usually stay in init
            for k in ["numres", "hide", "minsim", "db", "output_type", "testmode"]:
                if k in extra_params:
                    init_kwargs[k] = extra_params.pop(k)
        
        elif engine == "EHentai":
            # EHentai might need cookies or specific settings
            if "cookies" in extra_params:
                init_kwargs["cookies"] = extra_params.pop("cookies")

        # Create instance
        client = engine_cls(**init_kwargs)

        # 3. Prepare Search Arguments
        search_kwargs = {}
        
        # Check if source is URL or Base64
        # Simple heuristic: URL starts with http/https
        if source.startswith("http://") or source.startswith("https://"):
            search_kwargs["url"] = source
        else:
            # Assume Base64
            # Remove header if present (e.g., "data:image/png;base64,")
            if "," in source:
                source = source.split(",")[1]
            try:
                image_bytes = base64.b64decode(source)
                search_kwargs["file"] = image_bytes
            except Exception as e:
                return f"Error decoding Base64 string: {str(e)}"
        
        # Merge remaining extra params into search arguments
        search_kwargs.update(extra_params)

        # 4. Execute Search
        # PicImageSearch engines rely on aiohttp/httpx, so they are async
        resp = await client.search(**search_kwargs)

        # 5. Format Result
        # Most engines return a response object with .raw or similar
        # We'll rely on the object's string representation or convert to dict if possible
        
        # Custom formatting based on engine could be better, but generic str() is a start
        # PicImageSearch responses usually have a nice __str__ or we can extract data.
        
        result_str = f"Search Source: {engine}\n"
        if hasattr(resp, "origin"):
             result_str += f"Origin: {resp.origin}\n"
             
        # Generic extraction of results
        # Assuming most responses have 'raw' list
        if hasattr(resp, "raw") and resp.raw:
            result_str += f"Found {len(resp.raw)} results:\n"
            for i, item in enumerate(resp.raw):
                result_str += f"\n--- Result {i+1} ---\n"
                
                # Common fields try-out
                if hasattr(item, "title"):
                    result_str += f"Title: {item.title}\n"
                if hasattr(item, "url"):
                    result_str += f"URL: {item.url}\n"
                if hasattr(item, "similarity"):
                    result_str += f"Similarity: {item.similarity}%\n"
                if hasattr(item, "thumbnail"):
                    result_str += f"Thumbnail: {item.thumbnail}\n"
                if hasattr(item, "author"):
                    result_str += f"Author: {item.author}\n"
                
                # Fallback for other fields
                # result_str += f"Details: {item}\n"
        else:
            result_str += f"No results found or raw data unavailable.\nResponse: {resp}"
            
        return result_str

    except Exception as e:
        return f"An error occurred during search: {str(e)}\n{traceback.format_exc()}"

@mcp.tool()
async def search_image(
    engine: str, 
    source: str, 
    api_key: Optional[str] = None, 
    proxy: Optional[str] = None,
    extra_params_json: Optional[str] = None
) -> str:
    """
    Perform a reverse image search.
    
    Args:
        engine: The name of the search engine (e.g., "SauceNAO", "Google").
        source: The image URL or Base64 encoded string.
        api_key: (Optional) API key for engines that require it (e.g., SauceNAO).
        proxy: (Optional) Proxy URL (e.g., "http://127.0.0.1:7890").
        extra_params_json: (Optional) JSON string containing extra parameters for the search 
                           (e.g., '{"numres": 10, "hide": 0}').
    """
    return await _search_image_logic(engine, source, api_key, proxy, extra_params_json)

if __name__ == "__main__":
    mcp.run()
