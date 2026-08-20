"""HTTP server entrypoint facade kept separate from CLI argument handling."""


def create_server(address, handler):
    from http.server import HTTPServer
    try:
        from http.server import ThreadingHTTPServer
    except ImportError:
        ThreadingHTTPServer = HTTPServer
    return ThreadingHTTPServer(address, handler)
