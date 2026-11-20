#!/usr/bin/env python3
"""Browser-level proxy test for github.com/signup.

Why this exists: the earlier runner scan used a COLD GET /signup, which DataDome
always answers 403 even from a perfectly good IP (locally verified: cold GET is
403/770 while the home -> signup -> home -> click-link sequence yields the real
302 KB form). So "all free proxies are blocked" derived from cold GETs was
methodologically invalid. This script runs the real sequence per proxy and
records whether the form appears AND whether the POST passes DataDome.

Output: prox_browse.jsonl  (one JSON object per proxy, appended)
"""
import json, os, random, socket, ssl, string, sys, time
from concurrent.futures import ThreadPoolExecutor

SOURCES = [
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    "https://raw.githubusercontent.com/zloi-user/hideip.me/main/http.txt",
    "https://raw.githubusercontent.com/zloi-user/hideip.me/main/https.txt",
    "https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/http.txt",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/https.txt",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
    "https://raw.githubusercontent.com/mmpx12/proxy-list/master/https.txt",
    "https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt",
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt",
]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def log(*a):
    print(*a, flush=True)


def load_proxies():
    import urllib.request
    seen, out = set(), []
    for u in SOURCES:
        try:
            raw = urllib.request.urlopen(u, timeout=30).read().decode("utf-8", "replace")
        except Exception as e:
            log(f"[src] {u.split('/')[-2]} FAIL {type(e).__name__}")
            continue
        n = 0
        for line in raw.splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            host, _, port = line.partition(":")
            port = port.strip()
            if not port.isdigit():
                continue
            key = f"{host}:{port}"
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
            n += 1
        log(f"[src] {u.split('/')[-2]} +{n} total={len(out)}")
    return out


def connect_ok(px, timeout=6.0):
    host, _, port = px.partition(":")
    try:
        s = socket.create_connection((host, int(port)), timeout=timeout)
        s.sendall(b"CONNECT github.com:443 HTTP/1.1\r\nHost: github.com:443\r\n\r\n")
        s.settimeout(timeout)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(512)
            if not chunk:
                break
            buf += chunk
        ok = buf.startswith(b"HTTP/1.") and b" 200" in buf.split(b"\r\n")[0]
        s.close()
        return ok
    except Exception:
        return False


def rnd(n=10):
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))


def test_one(browser, px, mail_domain, budget=60.0):
    t0 = time.time()
    rec = {"px": px, "stage": "start"}
    ctx = None
    try:
        ctx = browser.new_context(proxy={"server": "http://" + px}, user_agent=UA,
                                  locale="en-US", viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.set_default_timeout(15000)
        page.goto("https://github.com/", wait_until="domcontentloaded", timeout=20000)
        rec["home"] = page.title()[:40]
        # cold visit seeds the datadome cookie (expected 403 page)
        try:
            page.goto("https://github.com/signup", wait_until="domcontentloaded", timeout=20000)
        except Exception:
            pass
        page.goto("https://github.com/", wait_until="domcontentloaded", timeout=20000)
        link = page.query_selector("a[href*='/signup']")
        if not link:
            rec["stage"] = "no_link"
            return rec
        link.click()
        form = False
        for _ in range(25):
            if time.time() - t0 > budget:
                break
            if page.query_selector("#email"):
                form = True
                break
            page.wait_for_timeout(1000)
        rec["signup_len"] = len(page.content())
        if not form:
            rec["stage"] = "no_form"
            return rec
        rec["stage"] = "form"
        uname = "wbp" + rnd(8)
        page.fill("#email", f"wbprobe{rnd(10)}@{mail_domain}")
        page.fill("#password", "Px!" + rnd(18) + "aZ9")
        for sel in ("#login", "input[name='user[login]']", "input[placeholder='Username']"):
            try:
                if page.query_selector(sel):
                    page.fill(sel, uname)
                    break
            except Exception:
                continue
        btn = None
        for _ in range(25):
            btn = page.query_selector("button.js-octocaptcha-form-submit")
            if btn:
                try:
                    if not page.eval_on_selector("button.js-octocaptcha-form-submit", "e=>e.disabled"):
                        break
                except Exception:
                    pass
            page.wait_for_timeout(1000)
        if not btn:
            rec["stage"] = "no_btn"
            return rec
        got = {}

        def on_resp(r):
            try:
                if r.request.method == "POST" and "/signup" in r.url:
                    got["status"] = r.status
                    got["loc"] = (r.headers or {}).get("location", "")[:120]
            except Exception:
                pass

        page.on("response", on_resp)
        try:
            btn.click(timeout=8000)
        except Exception as e:
            rec["click_err"] = type(e).__name__
        for _ in range(20):
            if "status" in got or page.url.rstrip("/").endswith("signup"):
                if "status" in got:
                    break
            page.wait_for_timeout(1000)
            if time.time() - t0 > budget + 40:
                break
        rec["post"] = got.get("status")
        rec["loc"] = got.get("loc", "")
        rec["url"] = page.url[:110]
        rec["stage"] = "posted"
        return rec
    except Exception as e:
        rec["stage"] = "exc"
        rec["err"] = f"{type(e).__name__}: {str(e)[:90]}"
        return rec
    finally:
        rec["sec"] = round(time.time() - t0, 1)
        try:
            if ctx:
                ctx.close()
        except Exception:
            pass


def main():
    budget = float(os.environ.get("BROWSE_BUDGET_MIN", "240")) * 60
    max_proxies = int(os.environ.get("MAX_PROXIES", "320"))
    mail_domain = os.environ.get("MAIL_DOMAIN", "example.com")
    t_start = time.time()
    allpx = load_proxies()
    log(f"COUNT={len(allpx)}")
    alive = []
    with ThreadPoolExecutor(max_workers=400) as ex:
        for px, ok in zip(allpx, ex.map(connect_ok, allpx)):
            if ok:
                alive.append(px)
    log(f"CONNECT_OK={len(alive)}")
    random.shuffle(alive)
    alive = alive[:max_proxies]
    log(f"TO_BROWSE={len(alive)}")

    from playwright.sync_api import sync_playwright
    out = open("prox_browse.jsonl", "a", encoding="utf-8")
    counts = {}
    hits = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox", "--disable-dev-shm-usage",
        ])
        for i, px in enumerate(alive):
            if time.time() - t_start > budget:
                log(f"[stop] budget reached at {i}")
                break
            rec = test_one(browser, px, mail_domain)
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()
            counts[rec["stage"]] = counts.get(rec["stage"], 0) + 1
            if rec.get("post") and 200 <= int(rec["post"]) < 400:
                hits += 1
                log(f"[HIT] {px} {rec.get('post')} {rec.get('url')}")
            else:
                log(f"[{i+1}/{len(alive)}] {px} {rec['stage']} post={rec.get('post')} {rec.get('sec')}s")
        browser.close()
    log(f"DONE stages={counts} POST_OK={hits}")


if __name__ == "__main__":
    main()
