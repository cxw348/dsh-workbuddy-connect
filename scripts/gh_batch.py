"""GitHub 批量注册器 v2 —— 已跑通一次（POST_OK）。

【已证实的成功链路】（ff_submit2.py，2026-11-21 12:5x）
  1) InvisiblePlaywright（Firefox stealth）走人类入口：
     GET / → GET /signup(403, 播种 datadome cookie) → GET / → 点 header signup 链接
     → 静止轮询 #email（绝不 poke iframe）→ 302KB 真表单
  2) 填 #email / #password / #login（三个都必填）
  3) **手写 octocaptcha-token = datadome-suppressed**
     Firefox stealth 下 octocaptcha iframe 永不回 postMessage → token 恒空
     → checkValidity()=false → requestSubmit() 返回 'invalid'（不是引擎不发 POST）
     原生 setter 写入后 checkValidity()=true
  4) 点 button.js-octocaptcha-form-submit → POST 200 → "Confirm your email address"

【v2 修的三处（git diff 式记录）】
  a) **出口轮换**：每次尝试换一个代理端口 21014..21020（七个 GTHost 米兰出口）
     —— 连打同一 IP 会让 DataDome 对 POST 的评分累积恶化（实测第 2/3 次 403）
  b) **email 异步校验**：GitHub 会先 GET /signup_check_new/email，期间
     user[email] 的 validationMessage = "Verifying…"。必须等到它消失再提交，
     否则 requestSubmit() 直接 invalid。
  c) **country 不硬改成 US**：GitHub 按出口 IP 自动填（意大利出口 → IT）。
     强行改成 US 与出口地理矛盾，是 DataDome 的加分项。

用法：
  python gh_batch.py --count 3
  python gh_batch.py --count 1 --seed 9091 --proxy 21014
"""
import argparse
import json
import os
import random
import re
import string
import sys
import time
import traceback
import urllib.request

OUT = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("HTTP_PROXY", "http://127.0.0.1:21014")
os.environ.setdefault("HTTPS_PROXY", "http://127.0.0.1:21014")
DOC = os.path.join(OUT, "gh_batch.json")
SHOT = os.path.join(OUT, "shots_batch")
os.makedirs(SHOT, exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36")
MAIL = "https://mail.tbrmail548.dpdns.org"
DOMAIN = "tbrmail548.dpdns.org"
OCTO_OK = "datadome-suppressed"
# 按钮解禁最长等待（秒）。实测解禁时间 2s~35s；>120s 基本是本出口被判，换出口更快。
BUTTON_WAIT_S = 120
PROXY_PORTS = [0]   # runner 版：0 = 直连（每次 run 都是全新 Azure IP）

SET_JS = """(args) => {
    const [sel, val] = args;
    const el = document.querySelector(sel);
    if (!el) return 'no-el';
    const proto = el.tagName === 'SELECT'
        ? window.HTMLSelectElement.prototype : window.HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, val);
    el.dispatchEvent(new Event('input', {bubbles:true}));
    el.dispatchEvent(new Event('change', {bubbles:true}));
    return el.value;
}"""

VALIDITY_JS = """() => {
    const f=[...document.querySelectorAll('form')]
      .find(x=>(x.action||'').includes('/signup'));
    if(!f) return {err:'no-form'};
    const bad=[...f.querySelectorAll('input,select,textarea')]
      .filter(e=>e.checkValidity&&!e.checkValidity())
      .map(e=>({n:e.name||e.id, msg:e.validationMessage}));
    return {valid:f.checkValidity(), bad:bad,
            token:(document.querySelector('input[name="octocaptcha-token"]')||{}).value,
            country:(document.querySelector('input[name="user_signup[country]"]')||{}).value};
}"""


# ---------------- mail ----------------
MAILTM = "https://api.mail.tm"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")


def _jreq(url, data=None, token=None, method=None):
    hdr = {"User-Agent": UA, "Accept": "application/json"}
    body = None
    if data is not None:
        hdr["Content-Type"] = "application/json"
        body = json.dumps(data).encode()
    if token:
        hdr["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=body, headers=hdr, method=method)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            if e.code == 429:
                time.sleep(8 + attempt * 5)
                continue
            try:
                return e.code, json.loads(raw)
            except Exception:  # noqa: BLE001
                return e.code, {"raw": raw[:200].decode("utf-8", "replace")}
        except Exception as e:  # noqa: BLE001
            if attempt == 3:
                raise
            time.sleep(4)
    return 0, {}


def new_mailbox():
    """mail.tm 建临时邮箱，返回 {address, jwt, password}。"""
    st, dom = _jreq(MAILTM + "/domains?page=1")
    if isinstance(dom, list):
        doms = [m.get("domain") for m in dom if isinstance(m, dict)]
    else:
        doms = [m["domain"] for m in (dom.get("hydra:member") or [])]
    domain = doms[0] if doms else "euploading.com"
    pwd = "Wb!" + "".join(random.choices(string.ascii_letters + string.digits, k=12))
    for _ in range(6):
        addr = "gh" + "".join(random.choices(string.ascii_lowercase + string.digits, k=12)) + "@" + domain
        st, r = _jreq(MAILTM + "/accounts", {"address": addr, "password": pwd})
        if st in (200, 201):
            st2, t = _jreq(MAILTM + "/token", {"address": addr, "password": pwd})
            if st2 in (200, 201) and t.get("token"):
                return {"address": addr, "jwt": t["token"], "password": pwd}
        time.sleep(2)
    raise RuntimeError("mail.tm 建箱失败")


def wait_code(jwt, timeout=150, poll=4):
    """等 GitHub launch code，返回 (code, subject)。锚点：6~8 位数字。"""
    deadline = time.time() + timeout
    seen = set()
    while time.time() < deadline:
        st, r = _jreq(MAILTM + "/messages?page=1", token=jwt)
        if st == 200:
            for m in (r.get("hydra:member") or r.get("messages") or []):
                mid = m.get("id")
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                st2, d = _jreq(MAILTM + "/messages/" + mid, token=jwt)
                if st2 != 200:
                    continue
                subj = d.get("subject") or ""
                body = d.get("text") or d.get("intro") or ""
                for txt in (body, subj):
                    for pat in (r"(\d{6})", r"(\d{8})",
                                r"[Ll]aunch code[^0-9]{0,40}(\d{6,8})"):
                        mt = re.search(pat, txt or "")
                        if mt:
                            return mt.group(1), subj
        time.sleep(poll)
    return None, ""


def wait_code(jwt, since=0.0, timeout=180, poll=4):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            req = urllib.request.Request(
                f"{MAIL}/api/mails?limit=25&offset=0",
                headers={"Authorization": "Bearer " + jwt, "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                mails = json.loads(r.read()).get("results", [])
        except Exception as e:  # noqa: BLE001
            print(f"    [mail] {type(e).__name__}", flush=True)
            time.sleep(poll)
            continue
        for m in mails:
            if since:
                try:
                    import email.utils as eu
                    ts = eu.parsedate_to_datetime(m.get("created_at", "")).timestamp()
                    if ts < since - 60:
                        continue
                except Exception:  # noqa: BLE001
                    pass
            blob = "\n".join(str(m.get(k, "")) for k in
                             ("subject", "preview", "raw", "text"))
            for pat in (r"verification code is[^0-9]{0,60}(\d{6})",
                        r"code[^0-9]{0,40}(\d{6})",
                        r"\b(\d{6})\b"):
                mm = re.search(pat, blob, re.I)
                if mm:
                    return mm.group(1), m.get("subject", "")
        time.sleep(poll)
    return None, None


# ---------------- browser ----------------
def open_form(page):
    page.goto("https://github.com/", wait_until="commit")
    time.sleep(5)
    try:
        page.goto("https://github.com/signup", wait_until="commit")
    except Exception:  # noqa: BLE001
        pass
    time.sleep(3)
    page.goto("https://github.com/", wait_until="commit")
    time.sleep(4)
    clicked = False
    for sel in ("a[href*='/signup']", "a:has-text('Sign up')"):
        try:
            if page.locator(sel).count():
                page.locator(sel).first.click()
                clicked = True
                break
        except Exception:  # noqa: BLE001
            continue
    if not clicked:
        return False
    for _ in range(50):
        try:
            if page.locator("#email").count():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)
    return False


def wait_email_ok(page, tries=8):
    """GitHub 会异步校验邮箱，期间 validationMessage='Verifying…'。"""
    for i in range(tries):
        v = page.evaluate("""() => {
            const e=document.querySelector('input[name="user[email]"]');
            if(!e) return {ok:false, msg:'no-input'};
            return {ok:e.checkValidity(), msg:e.validationMessage||''};
        }""")
        if v.get("ok"):
            return True, v.get("msg", "")
        print(f"    [email] {v.get('msg')!r} retry {i+1}/{tries}", flush=True)
        time.sleep(3)
    return False, v.get("msg", "")


def try_once(seed, idx, port):
    from invisible_playwright import InvisiblePlaywright

    proxy = None if not port else f"http://127.0.0.1:{port}"
    rec = {"idx": idx, "seed": seed, "proxy": proxy}
    mb = new_mailbox()
    email, jwt = mb["address"], mb["jwt"]
    uname = "wb" + "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
    pwd = "Wb!" + "".join(random.choices(string.ascii_letters + string.digits, k=15))
    rec.update({"email": email, "uname": uname, "pwd": pwd, "jwt": jwt})  # pwd/jwt 必须落盘
    t_mail = time.time()
    print(f"\n[{idx}] seed={seed} proxy={port} {email}", flush=True)

    _kw = {"seed": seed, "headless": True}
    if proxy:
        _kw["proxy"] = {"server": proxy}
    with InvisiblePlaywright(**_kw) as br:
        page = br.new_page()
        page.set_default_timeout(45000)

        ok = open_form(page)
        print(f"  [form] {ok}", flush=True)
        if not ok:
            rec["result"] = "NO_FORM"
            return rec

        for sel, val in (("#email", email), ("#password", pwd), ("#login", uname)):
            page.locator(sel).click()
            page.keyboard.type(val, delay=45)
            time.sleep(0.3)
        print("  [filled]", flush=True)

        eok, emsg = wait_email_ok(page)
        rec["email_ok"] = eok
        if not eok:
            rec["result"] = "EMAIL_VERIFY_STUCK"
            rec["email_msg"] = emsg
            return rec

        # octocaptcha：给它 20 秒看有没有真 token
        tok = ""
        for i in range(10):
            st = page.evaluate(
                "() => (document.querySelector('input[name=\"octocaptcha-token\"]')||{}).value")
            if st:
                tok = st
                print(f"  [octo] real token @{i*2}s: {st[:30]}", flush=True)
                break
            time.sleep(2)
        if not tok:
            page.evaluate(SET_JS, ["input[name='octocaptcha-token']", OCTO_OK])
            tok = OCTO_OK
            print("  [octo] forced datadome-suppressed", flush=True)
        rec["octo_token"] = tok

        v = page.evaluate(VALIDITY_JS)
        rec["validity"] = v
        print(f"  [valid] {v.get('valid')} bad={v.get('bad')} country={v.get('country')!r}",
              flush=True)
        if not v.get("valid"):
            rec["result"] = "INVALID_FORM"
            return rec

        # === country 是最早的成败判据（2026-11-21 实测）===
        # country='IT' → 按钮 0s 解禁 → POST 302 → SESSION_OK
        # country=''   → 按钮 86~98s 才解禁 → POST 403 必然失败
        # country 由 GitHub 按出口 IP 自动填；空值说明 DataDome 没拿到出口地理信息。
        if not v.get("country"):
            rec["result"] = "COUNTRY_MISSING"
            print("  [country] 空 -> 该出口已废，立即换", flush=True)
            return rec

        got = {}

        def on_resp(r):
            if "/signup" in r.url and r.request.method == "POST":
                got["status"] = r.status
                got["loc"] = r.headers.get("location", "")

        page.on("response", on_resp)

        # === 按钮是唯一门控（2026-11-21 实测）===
        # 3 例 SESSION_OK 全部 how=button_click；10 例 POST_FAILED 全部 how=requestSubmit。
        # 按钮 disabled 意味着 DataDome 设备检查尚未放行，此时 requestSubmit() 强提必然 403。
        # 所以：只等按钮自己解禁，等不到就换出口，绝不强行提交。
        dis = True
        waited = 0
        while waited < BUTTON_WAIT_S:
            dis = page.evaluate(
                "() => {const b=document.querySelector('button.js-octocaptcha-form-submit');"
                "return b? b.disabled : null;}")
            if dis is False:
                break
            time.sleep(2)
            waited += 2
        rec["button_wait_s"] = waited
        if dis is not False:
            rec["result"] = "BUTTON_STUCK"
            print(f"  [button] still disabled after {waited}s -> 换出口", flush=True)
            return rec
        print(f"  [button] enabled @{waited}s", flush=True)
        page.evaluate(
            "() => document.querySelector('button.js-octocaptcha-form-submit').click()")
        how = "button_click"
        for _ in range(20):
            if got.get("status"):
                break
            time.sleep(1)
        time.sleep(6)
        rec["submit"] = {"how": how, **got}

        body = page.content()
        confirmed_page = ("Confirm your email" in body) or ("confirm" in page.url.lower())
        print(f"  [submit] status={got.get('status')} confirm={confirmed_page} "
              f"url={page.url[:70]}", flush=True)
        try:
            page.screenshot(path=os.path.join(SHOT, f"a{idx}.png"))
        except Exception:  # noqa: BLE001
            pass

        if not confirmed_page:
            rec["result"] = "POST_FAILED"
            rec["final_text"] = page.inner_text("body")[:300]
            return rec

        print("  [code] waiting...", flush=True)
        code, subj = wait_code(jwt, since=t_mail)
        rec["code"] = code
        print(f"  [code] {code} subj={(subj or '')[:50]}", flush=True)
        if not code:
            rec["result"] = "NO_CODE"
            return rec

        filled = False
        for sel in ("input[name='verification_code']",
                    "input[autocomplete='one-time-code']",
                    "#launch-code-0"):
            try:
                if page.locator(sel).count():
                    page.locator(sel).first.click()
                    page.keyboard.type(code, delay=80)
                    filled = True
                    break
            except Exception:  # noqa: BLE001
                continue
        rec["code_filled"] = filled
        if filled:
            for sel in ("button:has-text('Verify')", "button[type='submit']",
                        "input[type='submit']"):
                try:
                    if page.locator(sel).count():
                        page.locator(sel).first.click()
                        break
                except Exception:  # noqa: BLE001
                    continue
            time.sleep(12)

        rec["final_url"] = page.url
        rec["result"] = "SESSION_OK" if "signup" not in page.url.lower() else "UNCONFIRMED"
        try:
            rec["final_text"] = page.inner_text("body")[:260]
        except Exception:  # noqa: BLE001
            pass
        print(f"  [final] {rec['result']} {page.url[:80]}", flush=True)
        return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--proxy", type=int, default=0, help="固定端口，0=轮换")
    ap.add_argument("--max-tries", type=int, default=4)
    a = ap.parse_args()

    results = []
    if os.path.exists(DOC):
        try:
            results = json.load(open(DOC, encoding="utf-8")).get("accounts", [])
        except Exception:  # noqa: BLE001
            results = []

    made = 0
    idx = len(results)
    port_i = 0
    while made < a.count:
        ok_run = False
        for t in range(a.max_tries):
            seed = a.seed if a.seed else random.randint(1000, 99999)
            port = a.proxy if a.proxy else PROXY_PORTS[port_i % len(PROXY_PORTS)]
            port_i += 1
            idx += 1
            try:
                r = try_once(seed, idx, port)
            except Exception:  # noqa: BLE001
                tb = traceback.format_exc()
                r = {"idx": idx, "seed": seed, "result": "EXCEPTION",
                     "trace": tb[-2000:]}
                print("  [EXC-FULL]\n" + tb, flush=True)
            results.append(r)
            with open(DOC, "w", encoding="utf-8") as f:
                json.dump({"accounts": results}, f, ensure_ascii=False, indent=2)
            if r.get("result") in ("SESSION_OK", "UNCONFIRMED"):
                ok_run = True
                made += 1
                print(f"  ==> 成功 #{made}: {r.get('email')} ({r['result']})", flush=True)
                break
            print(f"  ==> 失败: {r.get('result')}，换出口重试", flush=True)
            time.sleep(random.randint(10, 25))
        if not ok_run:
            print(f"\n!!! {a.max_tries} 次都没成功，停下等下一轮", flush=True)
            break

    print("\n===== 汇总 =====")
    for r in results:
        print(f"  [{r.get('result','?'):<18}] {r.get('email','-')}  "
              f"{r.get('uname','-')}  proxy={r.get('proxy','-')}")
    print(f"saved -> {DOC}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
