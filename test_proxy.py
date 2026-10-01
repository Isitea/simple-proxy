"""python3 test_proxy.py — 실제 소켓으로 Host 분기, Host/Origin 바꿔 보내기, reload 를 확인한다."""
import asyncio
import os
import signal
import tempfile
from pathlib import Path

import proxy

CONFIG = """
[forward]
19000 = "127.0.0.1:19101"
"sci.local:19000" = "127.0.0.1:19102"
"only.local:19002" = "127.0.0.1:19102"
19001 = { target = "127.0.0.1:19000", host = "sci.local" }
19003 = "127.0.0.1:19103"
19004 = "127.0.0.1:19103"
19005 = "127.0.0.1:19103"
19006 = { target = "127.0.0.1:19103", host = "up.local:80", origin = "http://up.local" }
"""


async def upstream(tag, port):
    async def reply(r, w):
        body = f"{tag} {proxy.host_of(await r.readuntil(b'\r\n\r\n'))}".encode()
        w.write(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))
        w.close()
    return await asyncio.start_server(reply, "127.0.0.1", port)


async def echo(r, w):
    while data := await r.read(1024):
        w.write(data)
    w.close()


async def ping(r, w, msg=b"hi"):
    w.write(msg)
    return await asyncio.wait_for(r.read(len(msg)), 1)


async def fetch(port, host):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(f"GET / HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
    data = await r.read()
    w.close()
    return data


async def main():
    cfg = Path(tempfile.mkdtemp()) / "config.toml"
    cfg.write_text(CONFIG)
    ups = [await upstream("A", 19101), await upstream("B", 19102), await asyncio.start_server(echo, "127.0.0.1", 19103)]
    server = asyncio.create_task(proxy.serve(cfg))
    await asyncio.sleep(0.3)

    assert b"A x" in await fetch(19000, "x"), "기본 규칙"
    assert b"B sci.local" in await fetch(19000, "SCI.local:5"), "이름 분기 (대소문자, 포트 무시)"
    assert b"B sci.local" in await fetch(19001, "anything"), "Host 바꿔 보내기"
    assert b"421" in await fetch(19002, "nope"), "기본 규칙이 없으면 421"

    # 에코 상류라 바뀐 요청 바이트가 그대로 돌아온다. keep-alive 의 모든 요청, 본문(길이/chunked), Upgrade 뒤 원본 바이트.
    sent = (b"POST /a HTTP/1.1\r\nHost: x:1\r\nOrigin: http://x:2\r\nContent-Length: 5\r\n\r\nHost:"
            b"POST /b HTTP/1.1\r\nhost: x\r\nTransfer-Encoding: chunked\r\nOrigin: null\r\n\r\n3\r\nabc\r\n0\r\n\r\n"
            b"GET /c HTTP/1.1\r\nHost: x\r\n\r\n"
            b"GET /ws HTTP/1.1\r\nHost: x\r\nOrigin: http://x\r\nUpgrade: websocket\r\n\r\nHost: raw")
    want = (b"POST /a HTTP/1.1\r\nHost: up.local:80\r\nOrigin: http://up.local\r\nContent-Length: 5\r\n\r\nHost:"
            b"POST /b HTTP/1.1\r\nHost: up.local:80\r\nTransfer-Encoding: chunked\r\nOrigin: http://up.local\r\n\r\n3\r\nabc\r\n0\r\n\r\n"
            b"GET /c HTTP/1.1\r\nHost: up.local:80\r\n\r\n"
            b"GET /ws HTTP/1.1\r\nHost: up.local:80\r\nOrigin: http://up.local\r\nUpgrade: websocket\r\n\r\nHost: raw")
    r, w = await asyncio.open_connection("127.0.0.1", 19006)
    w.write(sent)
    assert await asyncio.wait_for(r.readexactly(len(want)), 1) == want, "요청마다 Host/Origin 바꿔 보내기"
    w.close()

    keep, change, remove = [await asyncio.open_connection("127.0.0.1", p) for p in (19003, 19004, 19005)]
    for r, w in (keep, change, remove):
        assert await ping(r, w) == b"hi"

    new = CONFIG.replace('19000 = "127.0.0.1:19101"', '19000 = "127.0.0.1:19102"')
    new = new.replace('19004 = "127.0.0.1:19103"', '19004 = "127.0.0.1:19101"').replace('19005 = "127.0.0.1:19103"\n', "")
    cfg.write_text(new)
    os.kill(os.getpid(), signal.SIGHUP)
    await asyncio.sleep(0.3)
    assert b"B x" in await fetch(19000, "x"), "reload 로 대상 변경"
    assert await ping(*keep, b"yo") == b"yo", "그대로인 규칙의 연결은 유지"
    for r, w in (change, remove):
        assert await asyncio.wait_for(r.read(), 1) == b"", "바뀐/삭제된 규칙의 연결은 끊김"

    server.cancel()
    for u in ups:
        u.close()
    print("ok")


asyncio.run(main())
