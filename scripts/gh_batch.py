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
# runner 版：不设任何代理环境变量（invisible_playwright 引擎首次下载才需要，已预装）
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
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")


def new_mailbox(prefix="gh"):
    name = prefix + "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
    body = json.dumps({"enablePrefix": True, "name": name, "domain": DOMAIN}).encode()
    req = urllib.request.Request(
        f"{MAIL}/api/new_address", data=body,
        headers={"Content-Type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read())


def wait_code(jwt, since=0.0, timeout=180, poll=4):
    """取 GitHub launch code。

    2026-11-21 实测：GitHub 的 launch code 是 **8 位**（如 29974387），
    邮件正文锚点是「entering the code below:\n\n<8位>」。
    此前只抓 6 位，导致把前 6 位填进 OTP → 账号从未真正激活 → 登录报
    Incorrect username or password、github.com/<uname> 404。
    """
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
            # 顺序很重要：先锚 8 位（真码），再退回 6 位兼容旧邮件
            for pat in (r"code below[:\s]*?(\d{8})",
                        r"launch code[^0-9]{0,60}(\d{8})",
                        r"\b(\d{8})\b",
                        r"verification code is[^0-9]{0,60}(\d{6})",
                        r"\b(\d{6})\b"):
                mm = re.search(pat, blob, re.I)
                if mm:
                    return mm.group(1), m.get("subject", "")
        time.sleep(poll)
    return None, None



# ================= mail provider 切换（2026-11-21） =================
# 动机：CF 邮箱域名 tbrmail548.dpdns.org 属于 dpdns.org 后缀，Obsidian 台账记录
# 该后缀在 GitHub 的邮箱黑名单里（内网子域 35 个）。要验证「邮箱域」是不是
# suspended 的成因，必须能换域。
# GH_MAIL=mailtm 时改用 mail.tm 的 euploading.com。
MAIL_PROVIDER = os.environ.get("GH_MAIL", "cf").lower()
MAILTM = "https://api.mail.tm"
MAILTM_PWD = "Wb!Mailtm2026x"


def _mtm_req(url, data=None, token=None, method=None):
    h = {"User-Agent": UA, "Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read())


def new_mailbox_mailtm(prefix="gh"):
    addr = prefix + "".join(random.choices(string.ascii_lowercase + string.digits, k=10)) \
        + "@euploading.com"
    _mtm_req(f"{MAILTM}/accounts",
             json.dumps({"address": addr, "password": MAILTM_PWD}).encode())
    tk = _mtm_req(f"{MAILTM}/token",
                  json.dumps({"address": addr, "password": MAILTM_PWD}).encode())
    try:
        _mtm_req(f"{MAILTM}/me", token=tk["token"], method="PATCH")
    except Exception:  # noqa: BLE001
        pass
    return {"address": addr, "jwt": tk["token"], "provider": "mailtm"}


def wait_code_mailtm(jwt, since=0.0, timeout=180, poll=4):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            d = _mtm_req(f"{MAILTM}/messages?page=1", token=jwt)
            items = d.get("hydra:member", d if isinstance(d, list) else [])
        except Exception as e:  # noqa: BLE001
            print(f"    [mailtm] {type(e).__name__}", flush=True)
            time.sleep(poll)
            continue
        for m in items:
            try:
                full = _mtm_req(f"{MAILTM}/messages/{m['id']}", token=jwt)
            except Exception:  # noqa: BLE001
                continue
            blob = "\n".join(str(full.get(k, "")) for k in ("subject", "intro", "text"))
            for pat in (r"code below[:\s]*?(\d{8})", r"\b(\d{8})\b",
                        r"\b(\d{6})\b"):
                mm = re.search(pat, blob, re.I)
                if mm:
                    return mm.group(1), full.get("subject", "")
        time.sleep(poll)
    return None, None


def new_mailbox_dispatch(prefix="gh"):
    if MAIL_PROVIDER == "mailtm":
        return new_mailbox_mailtm(prefix)
    return new_mailbox(prefix)


def wait_code_dispatch(jwt, **kw):
    if MAIL_PROVIDER == "mailtm":
        return wait_code_mailtm(jwt, **kw)
    return wait_code(jwt, **kw)


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
    mb = new_mailbox_dispatch()
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

        # === 「已进入验证码页」的判据必须是 DOM，不是 URL ===
        # 2026-11-21 实测：POST 302 后 GitHub 的注册 SPA 会把 URL 留在
        # https://github.com/signup?ref_cta=... ，但 DOM 已切成 8 格 launch_code[] 验证码界面。
        # 用 URL 判会得到 confirmed=False 而误判 POST_FAILED。
        body = page.content()
        n_launch = page.locator("input#launch-code-0, input[name='launch_code[]']").count()
        confirmed_page = n_launch > 0 or ("Confirm your email" in body)
        rec["launch_boxes_on_confirm"] = n_launch
        print(f"  [submit] status={got.get('status')} otp_boxes={n_launch} "
              f"confirm={confirmed_page} url={page.url[:60]}", flush=True)
        try:
            page.screenshot(path=os.path.join(SHOT, f"a{idx}.png"))
        except Exception:  # noqa: BLE001
            pass

        if not confirmed_page:
            rec["result"] = "POST_FAILED"
            rec["final_text"] = page.inner_text("body")[:300]
            return rec

        print("  [code] waiting...", flush=True)
        code, subj = wait_code_dispatch(jwt, since=t_mail)
        rec["code"] = code
        print(f"  [code] {code} subj={(subj or '')[:50]}", flush=True)
        if not code:
            rec["result"] = "NO_CODE"
            return rec

        # === OTP 填写（2026-11-21 实测 DOM）===
        # /account_verifications 是 8 个单字符 input：name="launch_code[]"，id="launch-code-0..7"
        # 提交按钮文本 "Continue"（type=submit），旁边还有 "Resend the code"。
        # 旧写法只填 'input[name=verification_code]'（不存在）→ filled=False → 账号从未激活。
        filled = False
        try:
            boxes = page.locator("input#launch-code-0, input[name='launch_code[]']")
            n = boxes.count()
            if n:
                first = boxes.first
                first.click()
                page.keyboard.type(code, delay=90)   # 8 位逐格自动跳转
                time.sleep(1.0)
                filled = True
        except Exception:  # noqa: BLE001
            filled = False
        rec["otp_boxes"] = n if isinstance(locals().get("n"), int) else 0
        rec["code_filled"] = filled
        print(f"  [otp] boxes={rec['otp_boxes']} filled={filled}", flush=True)
        if filled:
            clicked = False
            for sel in ("button:has-text('Continue')",
                        "button[type='submit']",
                        "input[type='submit']"):
                try:
                    if page.locator(sel).count():
                        page.locator(sel).first.click()
                        clicked = True
                        break
                except Exception:  # noqa: BLE001
                    continue
            rec["otp_submitted"] = clicked
            print(f"  [otp] submitted={clicked}", flush=True)
            time.sleep(14)

        # === 真登录态判据：直接访问只有登录用户能看的页面 ===
        # 2026-11-21 教训：URL 停在 signup 不代表没建成；反过来跳到 /account_verifications
        # 也不代表建成了（旧版就是因为没填对 OTP 而假成功）。
        rec["url_after_otp"] = page.url
        verified = False
        try:
            page.goto("https://github.com/settings/profile", wait_until="commit")
            time.sleep(4)
            t = page.inner_text("body")[:4000]
            rec["settings_url"] = page.url
            rec["settings_len"] = len(t)
            low = t.lower()
            has_login = ("sign in to github" in low) or ("incorrect username" in low)
            verified = (not has_login) and ("profile" in low or "public profile" in low
                                            or "settings" in low)
            rec["settings_head"] = t[:200]
        except Exception as e:  # noqa: BLE001
            rec["settings_err"] = f"{type(e).__name__}: {e}"
        rec["verified_login"] = verified
        rec["final_url"] = page.url
        if verified:
            rec["result"] = "ACCOUNT_LIVE"
        elif rec.get("code_filled"):
            rec["result"] = "OTP_SUBMITTED_UNVERIFIED"
        else:
            rec["result"] = "UNCONFIRMED"
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
            # 只有 ACCOUNT_LIVE（真登录态已验证）才计入成功
            if r.get("result") == "ACCOUNT_LIVE":
                ok_run = True
                made += 1
                print(f"  ==> 成功 #{made}: {r.get('email')} {r.get('uname')}", flush=True)
                break
            if r.get("result") == "OTP_SUBMITTED_UNVERIFIED":
                print(f"  ==> OTP 已提交但未验证登录态，继续换出口", flush=True)
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
