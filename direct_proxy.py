"""Loopback CONNECT transport; reuse existing sing-box direct socket mark.

No TLS interception, credentials, firewall changes, or IP rotation.
"""
import argparse
import ipaddress
import select
import socket
import socketserver


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        upstream = None
        try:
            self.connection.settimeout(20)
            parts = self.rfile.readline(4096).decode('ascii').split()
            if len(parts) != 3 or parts[0] != 'CONNECT':
                raise ValueError('CONNECT required')
            host, port = parts[1].rsplit(':', 1)
            allowed = ('avito.ru', 'avito.st', 'avitocdn.ru')
            if port != '443' or not any(host == d or host.endswith('.' + d) for d in allowed):
                raise ValueError('destination denied')
            for _ in range(64):
                if self.rfile.readline(8192) in (b'\r\n', b'\n', b''):
                    break
            else:
                raise ValueError('headers too long')
            addresses = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
            address = addresses[0][4]
            if not ipaddress.ip_address(address[0]).is_global:
                raise ValueError('nonpublic address')
            upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            upstream.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, self.server.mark)
            upstream.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, self.server.interface.encode() + b'\0')
            upstream.settimeout(20)
            upstream.connect(address)
            print(f'CONNECT {host} {address[0]} interface={self.server.interface} mark={hex(self.server.mark)}', flush=True)
            self.connection.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            while True:
                ready, _, _ = select.select([self.connection, upstream], [], [], 60)
                if not ready:
                    break
                for src in ready:
                    data = src.recv(65536)
                    if not data:
                        return
                    (upstream if src is self.connection else self.connection).sendall(data)
        except (OSError, ValueError, UnicodeError) as exc:
            print(type(exc).__name__, flush=True)
            try:
                self.connection.sendall(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n')
            except OSError:
                pass
        finally:
            if upstream is not None:
                upstream.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18081)
    parser.add_argument('--mark', type=lambda s: int(s, 0), default=0x2024)
    parser.add_argument('--interface', default='eno1')
    args = parser.parse_args()
    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True
    with Server(('127.0.0.1', args.port), Handler) as server:
        server.mark, server.interface = args.mark, args.interface
        server.serve_forever()
