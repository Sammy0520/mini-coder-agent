from __future__ import annotations

import os
import select
import socket
import socketserver


MAX_HEADER_BYTES = 64 * 1024


class ConnectProxyHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        header = bytearray()
        while b"\r\n\r\n" not in header and len(header) < MAX_HEADER_BYTES:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            header.extend(chunk)
        try:
            first_line = bytes(header).split(b"\r\n", 1)[0].decode("ascii")
            method, authority, _ = first_line.split(" ", 2)
            host, port_text = authority.rsplit(":", 1)
            port = int(port_text)
        except (UnicodeDecodeError, ValueError):
            self.request.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            return
        allowed = {
            item.strip().casefold()
            for item in os.environ.get("ALLOWED_HOSTS", "").split(",")
            if item.strip()
        }
        if method != "CONNECT" or host.casefold() not in allowed or port != 443:
            self.request.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            return
        try:
            upstream = socket.create_connection((host, port), timeout=20)
        except OSError:
            self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return
        with upstream:
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            sockets = (self.request, upstream)
            while True:
                readable, _, _ = select.select(sockets, (), (), 60)
                if not readable:
                    return
                for source in readable:
                    data = source.recv(64 * 1024)
                    if not data:
                        return
                    destination = upstream if source is self.request else self.request
                    destination.sendall(data)


class ThreadingConnectProxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    with ThreadingConnectProxy(("0.0.0.0", 3128), ConnectProxyHandler) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
