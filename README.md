# simple-proxy

설정대로 포트를 전달하는 TCP 리버스 프록시. `python3` 3.11+ 만 있으면 된다.

## 사용

```sh
s-proxy up        # 백그라운드 기동
s-proxy reload    # 설정 다시 읽기. 대상이 바뀌었거나 사라진 규칙의 연결만 끊고 나머지는 유지
s-proxy status    # 상태와 규칙
s-proxy down      # 종료
s-proxy run       # 포그라운드 실행
s-proxy config    # 설정 파일 경로 출력 (없으면 권장 위치). 편집: $EDITOR $(s-proxy config)
```

- 설정 파일 탐색 (먼저 찾은 것을 쓴다, 없으면 "proxy 설정이 없다" 하고 종료)
  1. `-c PATH`
  2. 실행 파일이 `~/.local/bin` 에 있으면 `~/.config/simple-proxy.isitea/config.toml`
  3. 그 밖의 위치면 실행 파일 옆 `config.toml`, 그다음 `~/.config/simple-proxy.isitea/config.toml`
- 로그: `~/.local/state/simple-proxy/proxy.log`

## 설정 예시

왼쪽이 받는 쪽, 오른쪽이 보낼 곳이다.

```toml
listen_host = "127.0.0.1"                    # 생략 시 127.0.0.1

[forward]
9000 = "192.0.2.10:8787"                 # 포트 전달 (프로토콜 무관, 바이트 그대로)
"0.0.0.0:9010" = "192.0.2.10:8010"       # IP 키는 그 주소로 받는다
```

**호스트명으로 나누기 (HTTP)** — 키가 호스트명이면 첫 요청의 `Host` 헤더로 고른다.
대소문자와 포트는 무시한다. 맞는 이름이 없으면 포트만 적은 규칙으로 가고, 그것도 없으면 `421` 이다.

```toml
[forward]
8000 = "127.0.0.1:8000"                      # 기본
"science.local:8000" = "127.0.0.1:8001"
"trial.local:1000"   = "127.0.0.1:4003"
```

**Host 바꿔 보내기 (HTTP)** — `localhost:9050` 으로 접속해도 상류에는 `Host: science.local` 로 보낸다.

```toml
[forward]
9050 = { target = "192.0.2.10:9000", host = "science.local" }
```

**원격 쪽 짝** — 위 규칙을 받아 `Host` 로 서비스를 나눈다. 이름은 헤더 값일 뿐이라 DNS 등록이 필요 없다.

```toml
# 원격의 ~/.config/simple-proxy.isitea/config.toml
listen_host = "192.0.2.10"               # 이름 규칙은 이 주소에서 받는다

[forward]
"science.local:9000" = "127.0.0.1:8000"
"trial.local:9000"   = "127.0.0.1:4003"
9000 = "127.0.0.1:8000"                      # 선택: 이름이 안 맞을 때
```

## 주의

- 호스트명 규칙과 Host 바꾸기는 **HTTP 전용**이다. 이 포트는 첫 요청 헤더를 읽고 나서 상류에 연결하므로 SSH 같은 프로토콜에는 쓰지 마라. HTTPS 는 `Host` 를 볼 수 없다.
- 라우팅과 Host 바꾸기는 **연결의 첫 요청**에만 적용된다.
- 호스트명 키는 따옴표가 필요하다. `listen_host` 의 해당 포트에서 받는다.
- 이미 `0.0.0.0:9000` 으로 listen 하는 프로세스가 있으면 같은 포트의 다른 주소는 바인드되지 않는다 (`ss -ltn | grep :9000`).

## 배포 (단일 파일)

```sh
mise run build                                  # dist/s-proxy (README 포함)
dist/s-proxy install                            # ~/.local/bin/s-proxy 로 복사 (이 장비)
scp dist/s-proxy remote:/tmp/ && ssh remote /tmp/s-proxy install    # 원격
s-proxy uninstall                               # ~/.local/bin/s-proxy 제거 (실행 중이면 거부)
s-proxy --help detail                           # 이 안내 출력
```
