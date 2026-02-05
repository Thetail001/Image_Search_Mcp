import argparse
import sys
import os
from .server import mcp

class AuthMiddleware:
    """Simple ASGI middleware for Bearer Token authentication."""
    def __init__(self, app, token):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            # Allow health checks or OPTIONS if needed, but for strict security check everything
            headers = dict(scope.get("headers", []))
            auth_header = headers.get(b"authorization", b"").decode("utf-8")
            
            # Check for "Bearer <token>"
            expected = f"Bearer {self.token}"
            if not auth_header or auth_header != expected:
                await send({
                    'type': 'http.response.start',
                    'status': 401,
                    'headers': [(b'content-type', b'text/plain')],
                })
                await send({
                    'type': 'http.response.body',
                    'body': b'Unauthorized: Invalid or missing Bearer token',
                })
                return
        
        # Pass through to the original app
        await self.app(scope, receive, send)

def main():
    parser = argparse.ArgumentParser(description="Image Search MCP Server")
    parser.add_argument("--sse", action="store_true", help="Run in SSE mode (HTTP)")
    parser.add_argument("--port", type=int, default=8000, help="Port for SSE mode")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host for SSE mode")
    
    args = parser.parse_args()

    if args.sse:
        try:
            import uvicorn
            # Attempt to find the ASGI app from FastMCP instance
            app = mcp
            if hasattr(mcp, "_sse_app"):
                app = mcp._sse_app
            elif hasattr(mcp, "app"):
                app = mcp.app
            
            # Check for Auth Token
            auth_token = os.environ.get("MCP_AUTH_TOKEN")
            if auth_token:
                print(f"🔒 Authentication enabled. Require Bearer token.")
                app = AuthMiddleware(app, auth_token)
            
            print(f"Starting SSE server on {args.host}:{args.port}...")
            uvicorn.run(app, host=args.host, port=args.port)
        except ImportError:
            print("Error: 'uvicorn' is required for SSE mode. Please install it: pip install uvicorn")
            sys.exit(1)
        except Exception as e:
            print(f"Error starting SSE server: {e}")
            sys.exit(1)
    else:
        # Stdio mode (Default)
        mcp.run()

if __name__ == "__main__":
    main()
