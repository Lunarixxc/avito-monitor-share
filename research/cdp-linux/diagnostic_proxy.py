"""Research CONNECT transport: direct route, CAPTCHA dependencies, redacted logs."""
import argparse
import datetime as dt
import ipaddress
import json
import re
import select
import socket
import socketserver

# CAPTCHA scripts observed in the manual block page and confirmed in vendor docs.
ALLOWED = ('avito.ru', 'avito.st', 'avitocdn.ru', 'hcaptcha.com',
           'geetest.com', 'geevisit.com', 'gsensebot.com')


def allowed_destination(host, port):
    return port == '443' and any(host == d or host.endswith('.'+d) for d in ALLOWED)


def destination(line):
    parts = line.decode('ascii').split()
    if len(parts) != 3 or parts[0] != 'CONNECT':
        raise ValueError('invalid_connect')
    host, port = parts[1].rsplit(':', 1)
    if not re.fullmatch(r'[a-z0-9.-]{1,253}', host) or not port.isdigit():
        raise ValueError('invalid_destination')
    return host, port


def safe_domain(host):
    # Omit potentially identifying subdomains. No raw request text in logs.
    return '.'.join(host.split('.')[-2:]) if host else None


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        upstream = None
        tunnel = False
        stage, host, port = 'request', '', None
        def record(event, error=None):
            print(json.dumps(dict(time=dt.datetime.now(dt.timezone.utc).isoformat(),
                  event=event, stage=stage, domain=safe_domain(host), port=port,
                  error_type=error)), flush=True)
        try:
            self.connection.settimeout(20)
            line = self.rfile.readline(4096)
            if not line.strip():
                record('empty_probe')
                return
            host, port = destination(line)
            stage = 'allowlist'
            if not allowed_destination(host, port):
                record('denied')
                self.connection.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n')
                return
            stage = 'headers'
            for _ in range(64):
                if self.rfile.readline(8192) in (b'\r\n', b'\n', b''):
                    break
            else:
                raise ValueError('header_limit')
            stage = 'dns'
            address = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)[0][4]
            if not ipaddress.ip_address(address[0]).is_global:
                raise ValueError('nonpublic_address')
            stage = 'connect'
            upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            upstream.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, self.server.mark)
            upstream.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                                self.server.interface.encode()+b'\0')
            upstream.settimeout(20)
            upstream.connect(address)
            self.connection.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            tunnel, stage = True, 'tunnel'
            record('established')
            while True:
                ready, _, _ = select.select([self.connection, upstream], [], [], 60)
                if not ready:
                    record('idle_timeout')
                    return
                for src in ready:
                    data = src.recv(65536)
                    if not data:
                        record('closed')
                        return
                    (upstream if src is self.connection else self.connection).sendall(data)
        except (OSError, ValueError, UnicodeError) as exc:
            record('error', type(exc).__name__)
            if not tunnel:
                try:
                    self.connection.sendall(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n')
                except OSError:
                    pass
        finally:
            if upstream is not None:
                upstream.close()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=18081)
    parser.add_argument('--mark', type=lambda s: int(s, 0), default=0x2024)
    parser.add_argument('--interface', default='eno1')
    args = parser.parse_args()
    with Server(('127.0.0.1', args.port), Handler) as server:
        server.mark, server.interface = args.mark, args.interface
        server.serve_forever()
