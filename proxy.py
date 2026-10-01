#!/usr/bin/env python3
"""TCP 리버스 프록시. 사용법과 설정 예시: s-proxy --help detail"""
import argparse
import asyncio
import ipaddress
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import tomllib
import zipfile
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "simple-proxy"
PID, LOG = STATE / "proxy.pid", STATE / "proxy.log"
BIN = Path.home() / ".local/bin/s-proxy"
CONF = Path.home() / ".config/simple-proxy.isitea/config.toml"
CONNS = {}  # 진행 중인 연결 task -> (수신 주소, 규칙 이름, 연결한 Target). reload 때 바뀐 규칙의 연결을 끊는 데 쓴다.
MISDIRECTED = b"HTTP/1.1 421 Misdirected Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


class Target(NamedTuple):
    host: str
    port: int
    send_host: str | None = None  # 있으면 모든 요청의 Host 헤더를 이 값으로 바꿔 보낸다
    send_origin: str | None = None  # 있으면 모든 요청의 Origin 헤더(있을 때만)를 이 값으로 바꿔 보낸다


def log(msg):
    print(f"{datetime.now():%F %T} {msg}", flush=True)


# ---------------------------------------------------------------- 설정

def hostport(text):
    host, _, port = text.strip().rpartition(":")
    if not host:
        raise ValueError(f"'host:port' 형식이어야 한다: {text!r}")
    return host.strip("[]"), int(port)


def is_ip(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return host == "localhost"


def parse(data):
    """{(수신 호스트, 포트): {Host 이름 또는 None: Target}}"""
    default = data.get("listen_host", "127.0.0.1")
    if not data.get("forward"):
        raise ValueError("[forward] 에 규칙이 없다")
    routes = {}
    for key, val in data["forward"].items():
        try:
            host, name = default, None
            if ":" in key:
                h, port = hostport(key)
                if is_ip(h):
                    host = h
                else:
                    name = h.lower().rstrip(".")
            else:
                port = int(key)
            if isinstance(val, dict):
                if extra := val.keys() - {"target", "host", "origin"}:
                    raise ValueError(f"모르는 키 {sorted(extra)} (target, host, origin 만 쓸 수 있다)")
                target, send = val["target"], (val.get("host"), val.get("origin"))
            else:
                target, send = val, ()
            if not all(isinstance(v, str) for v in (target, *send) if v is not None):
                raise ValueError("값은 문자열이어야 한다")
            routes.setdefault((host, port), {})[name] = Target(*hostport(target), *send)
        except (ValueError, KeyError, AttributeError, TypeError) as e:
            raise ValueError(f"[forward] {key}: {e!r}") from None
    return routes


def load(path):
    return parse(tomllib.loads(Path(path).read_text()))


def describe(routes):
    return [f"{h}:{p} [{n or '*'}] -> {t.host}:{t.port}" + (f" (Host: {t.send_host})" if t.send_host else "")
            + (f" (Origin: {t.send_origin})" if t.send_origin else "")
            for (h, p), rules in routes.items() for n, t in rules.items()]


# ---------------------------------------------------------------- 프록시

def host_of(head):
    m = re.search(rb"(?im)^host:[ \t]*(\[[^\]]*\]|[^:\r\n]*)", head)
    return (m[1].decode("latin-1").strip("[]").lower().rstrip(".") or None) if m else None


def set_header(head, name, value):
    """head 의 name 헤더 값을 value 로 바꾼다. 헤더가 없으면 더하지 않는다."""
    line = name.encode() + b": " + value.encode("latin-1")
    return re.sub(rb"(?im)^" + name.encode() + rb":[^\r\n]*", lambda _: line, head, count=1)


def rewrite(head, t):
    if t.send_host:
        head = set_header(head, "Host", t.send_host)
    if t.send_origin:
        head = set_header(head, "Origin", t.send_origin)
    return head


async def read_head(reader):
    """다음 요청의 헤더. 클라이언트가 닫았으면 b""."""
    try:
        return await reader.readuntil(b"\r\n\r\n")
    except asyncio.IncompleteReadError:
        return b""


async def copy_n(reader, writer, n):
    while n:
        data = await reader.read(min(n, 65536))
        if not data:
            raise asyncio.IncompleteReadError(b"", n)
        writer.write(data)
        await writer.drain()
        n -= len(data)


async def copy_chunked(reader, writer):
    while True:
        line = await reader.readuntil(b"\r\n")
        writer.write(line)
        size = int(line.split(b";")[0], 16)
        if not size:
            break
        await copy_n(reader, writer, size + 2)  # 데이터 + CRLF
    while (line := await reader.readuntil(b"\r\n")) != b"\r\n":  # trailer
        writer.write(line)
    writer.write(line)


async def pipe_http(head, reader, writer, t):
    """요청마다 헤더를 바꿔 보내고 본문은 그대로 넘긴다. Upgrade(WebSocket 등) 요청 뒤로는 바이트 그대로."""
    try:
        while head:
            writer.write(rewrite(head, t))
            await writer.drain()
            if re.search(rb"(?im)^upgrade:", head):
                break
            if re.search(rb"(?im)^transfer-encoding:.*chunked", head):
                await copy_chunked(reader, writer)
            elif m := re.search(rb"(?im)^content-length:[ \t]*(\d+)", head):
                await copy_n(reader, writer, int(m[1]))
            head = await read_head(reader)
    except (ConnectionError, ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        return writer.close()
    await pipe(reader, writer)  # Upgrade 뒤, 또는 클라이언트가 쓰기를 닫았을 때 (곧바로 write_eof 하고 끝난다)


async def pipe(reader, writer):
    """EOF 를 받으면 반대쪽의 쓰기만 닫는다 (half-close). 소켓은 양방향이 다 끝난 뒤 handle 이 닫는다."""
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        writer.write_eof()
    except ConnectionError:
        writer.close()  # 한쪽이 깨지면 반대쪽도 끊어 다른 방향의 pipe 가 끝나게 한다


async def handle(routes, bind, cr, cw):
    rules = routes[bind]  # 접속 시점에 조회한다. 리로드는 다음 연결부터 반영된다.
    head = name = uw = None
    try:
        if any(n or t.send_host or t.send_origin for n, t in rules.items()):  # Host 를 보거나 바꿔야 하는 포트만 헤더를 읽는다
            head = await asyncio.wait_for(cr.readuntil(b"\r\n\r\n"), 10)
            name = host_of(head)
        t = rules.get(name) or rules.get(None)
        if not t:
            log(f"[{bind[1]}] Host {name!r} 에 맞는 규칙이 없다")
            cw.write(MISDIRECTED)
            return
        CONNS[asyncio.current_task()] = (bind, name if name in rules else None, t)
        ur, uw = await asyncio.open_connection(t.host, t.port)
        if t.send_host or t.send_origin:
            await asyncio.gather(pipe_http(head, cr, uw, t), pipe(ur, cw))
        else:
            if head:
                uw.write(head)
            await asyncio.gather(pipe(cr, uw), pipe(ur, cw))
    except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as e:
        log(f"[{bind[1]}] {type(e).__name__}: {e}")
    finally:
        CONNS.pop(asyncio.current_task(), None)
        cw.close()
        if uw:
            uw.close()


async def serve(path):
    routes, servers, stop = {}, {}, asyncio.Event()

    async def apply(first=False):
        new = load(path)
        for bind in servers.keys() - new.keys():
            servers.pop(bind).close()
            del routes[bind]
        for bind, rules in new.items():
            if bind not in servers:
                routes[bind] = rules
                try:
                    servers[bind] = await asyncio.start_server(
                        lambda r, w, b=bind: handle(routes, b, r, w), *bind)
                except OSError as e:
                    del routes[bind]
                    if first:
                        raise
                    log(f"{bind[0]}:{bind[1]} 바인드 실패, 건너뛴다: {e}")
                    continue
            routes[bind] = rules
        # 대상이 바뀌었거나 사라진 규칙의 연결은 끊는다. 클라이언트가 다시 붙으면 새 대상으로 간다.
        stale = [t for t, (b, key, target) in CONNS.items() if routes.get(b, {}).get(key) != target]
        for task in stale:
            task.cancel()
        for line in describe(new):
            log(line)
        if stale:
            log(f"reload: 연결 {len(stale)}개 끊음")

    async def reload():
        try:
            await apply()
        except Exception as e:  # 잘못된 설정으로 돌고 있는 프록시를 죽이지 않는다
            log(f"reload 실패, 기존 설정 유지: {e}")

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGHUP, lambda: loop.create_task(reload()))
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await apply(first=True)
    await stop.wait()


# ---------------------------------------------------------------- 프로세스 관리

def pid():
    """살아 있는 s-proxy 의 PID. PID 가 다른 프로세스에 재사용됐다면 None (엉뚱한 프로세스를 죽이지 않는다)."""
    try:
        p = int(PID.read_text())
        args = Path(f"/proc/{p}/cmdline").read_bytes().split(b"\0")
    except (OSError, ValueError):
        return None
    is_proxy = any(Path(a.decode(errors="replace")).name in ("s-proxy", "proxy.py") for a in args)
    return p if is_proxy and b"run" in args else None


def up(path):
    if p := pid():
        return print(f"already running (pid {p})")
    rules = load(path)  # 설정 오류는 로그가 아니라 이 터미널에서 먼저 잡는다
    STATE.mkdir(parents=True, exist_ok=True)
    with LOG.open("ab") as f:
        proc = subprocess.Popen([sys.executable, os.path.abspath(sys.argv[0]), "run", "-c", str(path)],
                                stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT,
                                start_new_session=True)
    PID.write_text(str(proc.pid))
    time.sleep(0.3)  # 바인드 실패처럼 곧바로 죽는 경우를 잡는다
    if proc.poll() is not None:
        sys.exit(f"기동 실패, 로그: {LOG}")
    print(f"started (pid {proc.pid}), log: {LOG}")
    print("\n".join(describe(rules)))


def down():
    p = pid() or sys.exit("not running")
    os.kill(p, signal.SIGTERM)
    for _ in range(90):
        if not pid():
            break
        time.sleep(0.1)
    else:  # 아직 살아 있다. PID 파일을 남겨 status/down 으로 다시 확인할 수 있게 한다
        sys.exit(f"SIGTERM timed out (pid {p}, 9s), still running")
    PID.unlink(missing_ok=True)
    print(f"stopped (pid {p})")


def config_candidates(explicit):
    """-c 지정 경로, 아니면 (~/.local/bin 밖이면) 실행 파일 옆 config.toml, 그다음 ~/.config. 마지막이 권장 위치."""
    exe_dir = Path(os.path.abspath(sys.argv[0])).parent
    if explicit:
        return [Path(explicit).expanduser()]
    return [CONF] if exe_dir == BIN.parent else [exe_dir / "config.toml", CONF]


def find_config(candidates):
    for p in candidates:
        if p.is_file():
            return p.resolve()
    sys.exit("proxy 설정이 없다. 아래 위치 중 한 곳에 만들어라:\n" + "\n".join(f"  {p}" for p in candidates))


def install():
    src = Path(sys.argv[0]).resolve()
    if not zipfile.is_zipfile(src):
        sys.exit("단일 파일(dist/s-proxy)로 실행할 때만 쓸 수 있다")
    if src == BIN.resolve():
        return print(f"이미 설치돼 있다: {BIN}")
    BIN.parent.mkdir(parents=True, exist_ok=True)
    tmp = BIN.with_name(BIN.name + ".tmp")  # 임시 파일을 만들어 교체한다. 실행 중인 프로세스는 옛 파일을 그대로 쓴다.
    try:
        shutil.copyfile(src, tmp)
        tmp.chmod(0o755)
        tmp.replace(BIN)
    except OSError:
        tmp.unlink(missing_ok=True)  # 디스크 부족 등으로 실패하면 반쯤 쓴 임시 파일을 남기지 않는다
        raise
    print(f"installed: {BIN}")
    if str(BIN.parent) not in map(os.path.normpath, os.environ.get("PATH", "").split(":")):
        print(f"주의: {BIN.parent} 가 PATH 에 없다")


def uninstall():
    if pid():
        sys.exit("실행 중이다. 먼저 s-proxy down")
    print(f"removed: {BIN} (설정과 로그는 그대로 둔다)" if BIN.exists() else "설치돼 있지 않다")
    BIN.unlink(missing_ok=True)


def detail():
    # abspath 를 쓰면 안 된다. zipimport 가 기록한 아카이브 경로 그대로여야 읽힌다.
    try:
        sys.stdout.write(__loader__.get_data(os.path.join(os.path.dirname(__file__), "README.md")).decode())
    except OSError:
        sys.exit("README.md 가 이 실행 파일에 들어 있지 않다")


def cli():
    if sys.argv[1:3] in (["-h", "detail"], ["--help", "detail"]):
        return detail()
    ap = argparse.ArgumentParser(prog="s-proxy", epilog="사용법과 설정 예시: s-proxy --help detail")
    ap.add_argument("command", choices=["up", "down", "reload", "status", "run", "config", "install", "uninstall"])
    ap.add_argument("-c", "--config", help="설정 파일 (기본: s-proxy --help detail 참고)")
    args = ap.parse_args()
    candidates = config_candidates(args.config)
    if args.command == "config":  # 설정이 없으면 권장 위치를 알려준다
        return print(next((p.resolve() for p in candidates if p.is_file()), candidates[-1]))
    path = None if args.command in ("down", "install", "uninstall") else find_config(candidates)
    try:
        match args.command:
            case "run":
                asyncio.run(serve(path))
            case "up":
                up(path)
            case "down":
                down()
            case "reload":
                load(path)  # 설정 오류를 이 터미널에서 먼저 잡는다
                os.kill(pid() or sys.exit("not running"), signal.SIGHUP)
                print("reloaded")
            case "install":
                install()
            case "uninstall":
                uninstall()
            case "status":
                p = pid()
                print(f"running (pid {p})" if p else "not running")
                print("\n".join(describe(load(path))))
                sys.exit(0 if p else 1)
    except (OSError, ValueError) as e:  # tomllib.TOMLDecodeError 는 ValueError
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    cli()
