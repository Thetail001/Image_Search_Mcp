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
            from starlette.applications import Starlette
            from starlette.routing import Route, Mount
            from mcp.server.sse import SseServerTransport

            # 1. Initialize SSE transport
            sse = SseServerTransport("/messages")

            # 2. Define the SSE handler as a raw ASGI app to get scope, receive, send
            async def handle_sse(scope, receive, send):
                async with sse.connect_sse(scope, receive, send) as (read_stream, write_stream):
                    # Use the underlying server from FastMCP instance
                    await mcp.server.run(
                        read_stream,
                        write_stream,
                        mcp.server.create_initialization_options()
                    )

            starlette_app = Starlette(
                debug=True,
                routes=[
                    # Mount handles the path prefix and passes raw ASGI arguments
                    Mount("/sse", app=handle_sse),
                    Route("/messages", endpoint=sse.handle_post_message, methods=["POST"]),
                ]
            )

            app = starlette_app
            
            # 3. Apply Auth Middleware if token is provided
            auth_token = os.environ.get("MCP_AUTH_TOKEN")
            if auth_token:
                print(f"🔒 Authentication enabled. Require Bearer token.")
                app = AuthMiddleware(app, auth_token)
            
            print(f"Starting SSE server on {args.host}:{args.port}...")
            uvicorn.run(app, host=args.host, port=args.port)
        except Exception as e:
            print(f"Error starting SSE server: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)
    else:
        # Stdio mode (Default)
        mcp.run()

if __name__ == "__main__":
    main()
