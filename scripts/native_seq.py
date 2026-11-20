#!/usr/bin/env python3
"""Release-sequence test on the runner's OWN egress IP (no proxy).

Local fact that motivates this: a COLD GET /signup always returns 403/770 even
from an IP that later yields the real 302 KB form. The cold-GET test therefore
cannot distinguish "bad IP" from "no session". The earlier runner scans all used
cold GETs -> their 403s proved nothing about the IP.

This runs the proven sequence (home -> signup (403, seeds cookie) -> home ->
click the signup link -> wait for #email) and then actually POSTs the form.
Reports: form length, whether POST happens, POST status, whether OTP page shows.
"""
import json, os, re, string, random, sys, time


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def log(*a):
    print(*a, flush=True)


def rnd(n=10):
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))


def run(mail, password, username):
    from playwright.sync_api import sync_playwright
    out = {"mail": mail, "user": username}
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=[
            "--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"])
        ctx = b.new_context(user_agent=UA, locale="en-US", viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.set_default_timeout(20000)
        page.on("console", lambda m: out.setdefault("console", []).append(m.text[:120]))
        page.goto("https://github.com/", wait_until="domcontentloaded")
        out["home_len"] = len(page.content())
        try:
            page.goto("https://github.com/signup", wait_until="domcontentloaded")
            out["cold_len"] = len(page.content())
        except Exception as e:
            out["cold_err"] = type(e).__name__
        page.goto("https://github.com/", wait_until="domcontentloaded")
        lk = page.query_selector("a[href*='/signup']")
        out["link"] = bool(lk)
        if lk:
            lk.click()
        got = False
        for _ in range(40):
            if page.query_selector("#email"):
                got = True
                break
            page.wait_for_timeout(1000)
        out["form"] = got
        out["signup_len"] = len(page.content())
        out["url"] = page.url[:120]
        if got:
            page.fill("#email", mail)
            page.fill("#password", password)
            for sel in ("#login", "input[name='user[login]']", "input[placeholder='Username']"):
                try:
                    if page.query_selector(sel):
                        page.fill(sel, username)
                        out["uname_sel"] = sel
                        break
                except Exception:
                    continue
            btn_ok = False
            for _ in range(40):
                try:
                    dis = page.eval_on_selector("button.js-octocaptcha-form-submit", "e=>e.disabled")
                except Exception:
                    dis = True
                if dis is False:
                    btn_ok = True
                    break
                page.wait_for_timeout(1000)
            out["btn_enabled"] = btn_ok
            resp = {}

            def on_resp(r):
                if r.request.method == "POST" and "/signup" in r.url:
                    resp["status"] = r.status
                    resp["loc"] = (r.headers or {}).get("location", "")[:150]

            page.on("response", on_resp)
            try:
                page.click("button.js-octocaptcha-form-submit", timeout=10000)
            except Exception as e:
                out["click_err"] = type(e).__name__
            for _ in range(45):
                if "status" in resp:
                    break
                page.wait_for_timeout(1000)
            out["post"] = resp.get("status")
            out["loc"] = resp.get("loc", "")
            page.wait_for_timeout(3000)
            body = page.content()
            out["after_url"] = page.url[:140]
            out["has_otp"] = bool(re.search(r'name="otp"|id="otp"|verify-email|confirm-email', body, re.I))
            out["text"] = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))[:600]
            try:
                page.screenshot(path="native_after.png", full_page=False)
            except Exception:
                pass
        b.close()
    return out


def main():
    mail = os.environ.get("GH_MAIL", f"wbprobe{rnd(10)}@example.com")
    pwd = os.environ.get("GH_PASS", "Nv!" + rnd(16) + "aZ9")
    user = os.environ.get("GH_USERNAME", "wbn" + rnd(8))
    rec = run(mail, pwd, user)
    log("RESULT " + json.dumps(rec, ensure_ascii=False))
    with open("native_seq.json", "w") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
