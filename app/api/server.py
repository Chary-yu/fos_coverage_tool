"""HTTP server entrypoint facade kept separate from CLI argument handling."""


def create_server(address, handler):
    from http.server import HTTPServer
    try:
        from http.server import ThreadingHTTPServer
    except ImportError:
        import socketserver

        class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
            daemon_threads = True
            allow_reuse_address = True
    return ThreadingHTTPServer(address, handler)
