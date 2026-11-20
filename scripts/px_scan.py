import os, sys, socket, ssl, json, time
import concurrent.futures as cf
from collections import Counter

LIMIT = int(os.environ.get("LIMIT", "6000"))
UA = b"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"


def test(px):
    host, port = px.rsplit(":", 1)
    try:
        s = socket.create_connection((host, int(port)), timeout=6)
    except Exception:
        return None
    try:
        s.settimeout(9)
        s.sendall(b"CONNECT github.com:443 HTTP/1.1\r\nHost: github.com:443\r\nUser-Agent: " + UA + b"\r\n\r\n")
        buf = b""
        while b"\r\n\r\n" not in buf:
            c = s.recv(4096)
            if not c:
                raise Exception("eof")
            buf += c
            if len(buf) > 8192:
                raise Exception("big")
        if b"200" not in buf.split(b"\r\n")[0]:
            raise Exception("connect")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        t = ctx.wrap_socket(s, server_hostname="github.com")
        t.settimeout(12)
        t.sendall(b"GET /signup HTTP/1.1\r\nHost: github.com\r\nUser-Agent: " + UA +
                  b"\r\nAccept: text/html,application/xhtml+xml\r\nAccept-Language: en-US,en;q=0.9\r\nConnection: close\r\n\r\n")
        data = b""
        while True:
            c = t.recv(65536)
            if not c:
                break
            data += c
            if len(data) > 400000:
                break
        parts = data.split(b"\r\n\r\n", 1)
        status = parts[0].split(b"\r\n")[0].decode("latin1", "replace")
        body = parts[1] if len(parts) > 1 else b""
        return {
            "px": px,
            "status": status,
            "len": len(body),
            "email": (b'id="email"' in body) or (b'name="user[email]"' in body),
            "dd": (b"datadome" in body) or (b"captcha-delivery" in body),
        }
    except Exception:
        return None
    finally:
        try:
            s.close()
        except Exception:
            pass


def main():
    px = [l.strip() for l in open("/tmp/all.txt") if l.strip()][:LIMIT]
    print("testing", len(px), flush=True)
    t0 = time.time()
    hits = []
    done = 0
    with cf.ThreadPoolExecutor(max_workers=200) as ex:
        for r in ex.map(test, px):
            done += 1
            if r:
                hits.append(r)
            if done % 500 == 0:
                print("progress", done, "hits", len(hits), int(time.time() - t0), "s", flush=True)
    print("DONE", done, "in", int(time.time() - t0), "s hits", len(hits), flush=True)
    open("/tmp/hits.json", "w").write(json.dumps(hits, indent=1))
    good = [h for h in hits if h["email"]]
    big = [h for h in hits if (not h["email"]) and h["len"] > 5000]
    print("EMAIL_OK", len(good), flush=True)
    for h in good[:40]:
        print(" OK", h, flush=True)
    print("BIG_NONEMAIL", len(big), flush=True)
    for h in big[:20]:
        print(" BIG", h, flush=True)
    print("STATUS", Counter([h["status"] for h in hits]).most_common(10), flush=True)


main()
