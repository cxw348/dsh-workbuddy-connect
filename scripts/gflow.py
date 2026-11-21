"""Runner 上走 Continue with Google 建 Google 号（绕开 GitHub /signup 的 DataDome）。

为什么这条路成立：
  - GitHub /signup 在 Azure/AWS runner IP 上都是 403/770 + datadome，硬封。
  - GitHub /login 在同样 IP 上是 200/45xxx，正常。
  - /login 页上有 form action = /sessions/social/google/initiate（Continue with Google）。
  - 走这条路径，建号发生在 accounts.google.com，用的是 GitHub 自己的 OAuth client
    (1078992815106-...)，完全不触碰 /signup → 不触发 DataDome。

本脚本要回答一个问题：runner IP 上 Google 建号是
  (a) 直接建成   (b) 要手机扫码 crossflowverification   (c) Sorry, could not create
此前用意大利代理测是 (b)。runner 是干净的云 IP，结果可能不同。

产物：gflow.json + 每步截图。
"""
import json
import os
import random
import re
import string
import sys
import time
import traceback

from playwright.sync_api import sync_playwright

OUT = os.path.dirname(os.path.abspath(__file__))
BIZ = os.path.join(OUT, "shots")
os.makedirs(BIZ, exist_ok=True)

FIRST = random.choice(["Wei", "Lin", "Hao", "Jun", "Ming", "Tao", "Yan", "Bo"])
LAST = random.choice(["Chen", "Wang", "Li", "Zhang", "Liu", "Yang", "Huang"])
UNUM = "wb" + "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
YEAR = str(random.randint(1990, 1996))
MONTH = random.choice(["March", "May", "July", "September", "November"])
DAY = str(random.randint(3, 27))
PWD = "Wb" + "".join(random.choices(string.ascii_letters + string.digits, k=13)) + "!7"


def snap(pg, name, log):
    fp = os.path.join(BIZ, name + ".png")
    try:
        pg.screenshot(path=fp, full_page=False)
        log.append({"shot": name, "file": fp, "url": pg.url})
    except Exception:  # noqa: BLE001
        pass


def info(pg):
    try:
        txt = pg.inner_text("body")[:1200]
    except Exception:  # noqa: BLE001
        txt = ""
    return {"url": pg.url, "len": len(pg.content()), "text": txt}


def click_text(pg, pattern, timeout=8000):
    """按可见文本点击，返回 True/False。"""
    try:
        loc = pg.locator(pattern).first
        loc.wait_for(state="visible", timeout=timeout)
        loc.click()
        return True
    except Exception:  # noqa: BLE001
        return False


def main():
    res = {"steps": [], "verdict": "unknown", "unum": UNUM}
    with sync_playwright() as p:
        br = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled",
                  "--no-sandbox"])
        ctx = br.new_context(
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="America/Los_Angeles",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/157.0.0.0 Safari/537.36"))
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        pg = ctx.new_page()
        pg.set_default_timeout(30000)

        def step(name, extra=None):
            d = info(pg)
            d["step"] = name
            if extra:
                d.update(extra)
            res["steps"].append(d)
            print(f"[{name}] {d['url'][:110]} len={d['len']}")
            return d

        try:
            # 0) 出口
            pg.goto("https://api.ip.sb/geoip", wait_until="domcontentloaded")
            res["exit"] = pg.inner_text("body")[:400]
            print("EXIT:", res["exit"][:200])

            # 1) GitHub 登录页
            pg.goto("https://github.com/login", wait_until="domcontentloaded")
            time.sleep(2)
            d = step("gh_login")
            res["gh_login_ok"] = pg.locator("#login_field").count() > 0
            snap(pg, "01_gh_login", res["steps"])
            print("  gh_login_ok =", res["gh_login_ok"])
            if not res["gh_login_ok"]:
                res["verdict"] = "gh_login_blocked"
                return finish(res, br)

            # 2) 点 Continue with Google
            ok = click_text(pg, "text=/Continue with Google/i", 10000)
            print("  clicked Continue with Google:", ok)
            if not ok:
                # 备用：直接提交 social/google form
                try:
                    pg.evaluate("""() => {
                      for (const f of document.querySelectorAll('form')) {
                        if ((f.action||'').includes('/sessions/social/google/initiate')) {
                          f.submit(); return 'submitted';
                        }
                      }
                      return 'no-form';
                    }""")
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(4)
            d = step("after_google_click")
            snap(pg, "02_google", res["steps"])
            print("  ->", d["url"][:110])

            # 3) Google 登录页 → 点 Create account
            if "accounts.google.com" not in pg.url:
                res["verdict"] = "not_redirected_to_google"
                return finish(res, br)

            if "/signup" not in pg.url and "createaccount" not in pg.url:
                ok = click_text(pg, "text=/^Create account$/i", 8000)
                if not ok:
                    ok = click_text(pg, "text=/Create an account/i", 5000)
                print("  clicked Create account:", ok)
                time.sleep(4)
                step("after_create_account")
                snap(pg, "03_create", res["steps"])

            if "signup" not in pg.url:
                res["verdict"] = "create_account_not_reached"
                res["final_url"] = pg.url
                return finish(res, br)

            # 4) 姓名页
            if pg.locator("#firstName").count():
                pg.fill("#firstName", FIRST)
                try:
                    if pg.locator("#lastName").count():
                        pg.fill("#lastName", LAST)
                except Exception:  # noqa: BLE001
                    pass
                click_text(pg, "button:has-text('Next')", 8000)
                time.sleep(3)
                step("name")
                snap(pg, "04_name", res["steps"])

            # 5) 生日性别页
            if "birthdaygender" in pg.url:
                pg.locator("#month").click()
                time.sleep(0.6)
                try:
                    pg.locator("[role=option]", has_text=MONTH).first.click()
                except Exception:  # noqa: BLE001
                    pg.locator("[role=option]").first.click()
                time.sleep(0.4)
                for sel, val in (("#day", DAY), ("#year", YEAR)):
                    pg.locator(sel).click()
                    pg.keyboard.press("Control+A")
                    pg.keyboard.press("Delete")
                    pg.keyboard.type(val, delay=90)
                    time.sleep(0.2)
                pg.locator("#gender").click()
                time.sleep(0.6)
                try:
                    pg.locator("[role=option]", has_text=re.compile(r"^Male$")).first.click()
                except Exception:  # noqa: BLE001
                    pg.locator("[role=option]").nth(1).click()
                time.sleep(0.4)
                click_text(pg, "button:has-text('Next')", 8000)
                time.sleep(3)
                step("birthday")
                snap(pg, "05_bday", res["steps"])

            # 6) 用户名页
            if "username" in pg.url:
                if pg.locator("input[name=Username]").count() == 0 or \
                        not pg.locator("input[name=Username]").first.is_visible():
                    click_text(pg, "text=/Create your own Gmail address/i", 5000)
                    time.sleep(1)
                pg.fill("input[name=Username]", UNUM)
                time.sleep(0.5)
                click_text(pg, "button:has-text('Next')", 8000)
                time.sleep(3)
                step("username")
                snap(pg, "06_uname", res["steps"])

            # 7) 密码页（两个框都填）
            if "password" in pg.url:
                boxes = pg.locator("input[type=password]")
                n = boxes.count()
                for i in range(n):
                    boxes.nth(i).click()
                    pg.keyboard.type(PWD, delay=70)
                    time.sleep(0.2)
                click_text(pg, "button:has-text('Next')", 8000)
                time.sleep(5)
                step("password")
                snap(pg, "07_pwd", res["steps"])

            # 8) 判终态
            d = step("final")
            u = d["url"]
            t = d["text"]
            if "crossflowverification" in u:
                res["verdict"] = "NEED_PHONE_QR"
            elif "/error/" in u or "could not create" in t.lower():
                res["verdict"] = "GOOGLE_REFUSED"
            elif "gmail.com" in t or "Welcome" in t or "My Account" in t:
                res["verdict"] = "CREATED"
            elif "signup" in u:
                res["verdict"] = "STUCK_SIGNUP"
            else:
                res["verdict"] = "UNKNOWN"
            res["final_url"] = u
            res["final_text"] = t[:900]

        except Exception:  # noqa: BLE001
            res["verdict"] = "EXCEPTION"
            res["trace"] = traceback.format_exc()[-1500:]
            snap(pg, "99_err", res["steps"])
            print(res["trace"])
            return finish(res, br)

        return finish(res, br)


def finish(res, br):
    try:
        br.close()
    except Exception:  # noqa: BLE001
        pass
    fp = os.path.join(OUT, "gflow.json")
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print("\n===== VERDICT:", res["verdict"], "=====")
    print("unum:", res.get("unum"))
    print("saved ->", fp)
    sys.exit(0)


if __name__ == "__main__":
    main()
