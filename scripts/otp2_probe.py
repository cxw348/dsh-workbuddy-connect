"""OTP 提交后到底发生什么 + 用密码另登一次作硬判据（runner 直连版）"""
import json, os, random, string, sys, time, urllib.request
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gh_batch as G
from invisible_playwright import InvisiblePlaywright

OUT = os.path.join(HERE, 'otp2'); os.makedirs(OUT, exist_ok=True)
UA = G.UA

def login_check(email, pwd):
    """独立 context 用 /login 验证账号是否真的存在且密码正确"""
    res = {'ok': False}
    try:
        with InvisiblePlaywright(seed=random.randint(1000, 99999), headless=True) as br:
            pg = br.new_page(); pg.set_default_timeout(40000)
            pg.goto('https://github.com/login', wait_until='commit'); time.sleep(5)
            pg.fill('#login_field', email)
            pg.fill('#password', pwd)
            pg.locator("input[type='submit'][name='commit']").first.click()
            time.sleep(10)
            res['url'] = pg.url
            t = pg.inner_text('body')
            res['text'] = t[:600]
            low = t.lower()
            res['suspended'] = 'suspended' in low
            res['bad_cred'] = 'incorrect username or password' in low
            res['otp_needed'] = ('device verification' in low) or ('two-factor' in low
                                ) or ('verification code' in low) or ('launch code' in low)
            res['ok'] = ('dashboard' in pg.url) or ('github.com/' in pg.url and 'login' not in pg.url
                                                    and not res['bad_cred'] and not res['suspended'])
            try: pg.screenshot(path=os.path.join(OUT, 'login.png'))
            except Exception: pass
    except Exception as e:  # noqa: BLE001
        res['err'] = f'{type(e).__name__}: {e}'
    return res

def probe(idx):
    mb = G.new_mailbox(); email, jwt = mb['address'], mb['jwt']
    uname = 'wb' + ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
    pwd = 'Wb!' + ''.join(random.choices(string.ascii_letters + string.digits, k=15))
    rec = {'idx': idx, 'email': email, 'uname': uname, 'pwd': pwd, 'jwt': jwt}
    print(f'\n[{idx}] {email} {uname} {pwd}', flush=True)
    t_mail = time.time()
    with InvisiblePlaywright(seed=random.randint(1000, 99999), headless=True) as br:
        pg = br.new_page(); pg.set_default_timeout(45000)
        if not G.open_form(pg):
            rec['result'] = 'NO_FORM'; return rec
        for sel, val in (('#email', email), ('#password', pwd), ('#login', uname)):
            pg.locator(sel).click(); pg.keyboard.type(val, delay=45); time.sleep(0.3)
        G.wait_email_ok(pg)
        pg.evaluate(G.SET_JS, ["input[name='octocaptcha-token']", G.OCTO_OK])
        v = pg.evaluate(G.VALIDITY_JS)
        print('  country=%r' % v.get('country'), flush=True)
        if not v.get('country'):
            rec['result'] = 'COUNTRY_MISSING'; return rec
        for i in range(G.BUTTON_WAIT_S // 2):
            if pg.evaluate("() => {const b=document.querySelector('button.js-octocaptcha-form-submit');return b?b.disabled:null;}") is False: break
            time.sleep(2)
        pg.evaluate("() => document.querySelector('button.js-octocaptcha-form-submit').click()")
        # 2026-11-21 实测：POST 之后 SPA 会先空白一段时间再渲染 8 格验证码。
        # 固定 sleep 12s 会在慢出口上抓到空 body（otp2 首跑就栽在这），必须显式等 selector。
        n = 0
        for _ in range(30):                      # 最多等 90 秒
            try:
                n = pg.locator("input#launch-code-0, input[name='launch_code[]']").count()
            except Exception:  # noqa: BLE001
                n = 0
            if n:
                break
            time.sleep(3)
        rec['otp_wait_boxes'] = n
        print('  otp boxes=%d url=%s' % (n, pg.url[:70]), flush=True)
        try: pg.screenshot(path=os.path.join(OUT, f'landing{idx}.png'), full_page=True)
        except Exception: pass
        if not n:
            rec['result'] = 'NO_OTP_PAGE'
            try: rec['text'] = pg.inner_text('body')[:600]
            except Exception: rec['text'] = ''
            rec['landing_url'] = pg.url
            print('  landing_text=%r' % rec['text'][:200], flush=True)
            return rec
        code, subj = G.wait_code(jwt, since=t_mail, timeout=150)
        rec['code'] = code
        print('  code=%s len=%d' % (code, len(code or '')), flush=True)
        if not code:
            rec['result'] = 'NO_CODE'; return rec
        # 逐格填
        for i, ch in enumerate(code):
            b = pg.locator(f'#launch-code-{i}')
            if b.count():
                b.click(); pg.keyboard.type(ch); time.sleep(0.12)
        vals = pg.evaluate("""() => [...document.querySelectorAll("input[name='launch_code[]']")].map(e=>e.value)""")
        rec['box_values'] = vals
        print('  box_values=%s' % (''.join(v or '' for v in vals)), flush=True)
        try: pg.screenshot(path=os.path.join(OUT, f'filled{idx}.png'), full_page=True)
        except Exception: pass
        # 找 Continue 并点
        btn = pg.locator("button[type='submit']:has-text('Continue')")
        rec['btn_count'] = btn.count()
        if btn.count():
            btn.first.click()
        else:
            pg.keyboard.press('Enter')
        time.sleep(15)
        rec['after_url'] = pg.url
        try: rec['after_text'] = pg.inner_text('body')[:800]
        except Exception: pass
        try: pg.screenshot(path=os.path.join(OUT, f'after{idx}.png'), full_page=True)
        except Exception: pass
        print('  after_url=%s' % pg.url[:80], flush=True)
        print('  after_text=%s' % (rec.get('after_text', '')[:250].replace('\n', ' | ')), flush=True)
        rec['result'] = 'OTP_DONE'
        return rec
    # 关闭浏览器后再独立登录验证
def after_login(rec):
    if rec.get('result') != 'OTP_DONE':
        return rec
    lc = login_check(rec['email'], rec['pwd'])
    rec['login'] = lc
    rec['result'] = 'LIVE' if lc.get('ok') else ('SUSPENDED' if lc.get('suspended')
        else ('BAD_CRED' if lc.get('bad_cred') else ('NEED_MORE' if lc.get('otp_needed') else 'LOGIN_FAIL')))
    print('  login => %s url=%s' % (rec['result'], lc.get('url', '')[:70]), flush=True)
    return rec

def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    out = []
    for i in range(1, n + 1):
        try:
            r = probe(i)
            r = after_login(r)
            out.append(r)
        except Exception:
            import traceback; tb = traceback.format_exc()
            print(tb, flush=True); out.append({'idx': i, 'result': 'EXC', 'trace': tb[-1500:]})
    json.dump({'probes': out}, open(os.path.join(HERE, 'otp2_probe.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    print('\n===== SUMMARY =====', flush=True)
    for r in out:
        print('  %-14s %s %s %s' % (r.get('result'), r.get('uname', '-'), r.get('pwd', '-'),
                                    (r.get('login') or {}).get('url', '-')[:50]), flush=True)

if __name__ == '__main__':
    main()
