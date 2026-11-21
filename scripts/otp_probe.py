"""探明 GitHub /account_verifications 真实 DOM 与 OTP 提交方式（runner 直连版）"""
import json, os, random, string, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gh_batch as G
from invisible_playwright import InvisiblePlaywright

OUT = os.path.join(HERE, 'otp_dump'); os.makedirs(OUT, exist_ok=True)

def probe(idx, seed):
    mb = G.new_mailbox(); email, jwt = mb['address'], mb['jwt']
    uname = 'wb' + ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
    pwd = 'Wb!' + ''.join(random.choices(string.ascii_letters + string.digits, k=15))
    rec = {'idx': idx, 'email': email, 'uname': uname, 'pwd': pwd, 'jwt': jwt}
    print(f'\n[{idx}] {email} {uname}', flush=True)
    t_mail = time.time()
    with InvisiblePlaywright(seed=seed, headless=True) as br:
        pg = br.new_page(); pg.set_default_timeout(45000)
        if not G.open_form(pg):
            rec['result'] = 'NO_FORM'; return rec
        for sel, val in (('#email', email), ('#password', pwd), ('#login', uname)):
            pg.locator(sel).click(); pg.keyboard.type(val, delay=45); time.sleep(0.3)
        eok, emsg = G.wait_email_ok(pg)
        rec['email_ok'] = eok
        pg.evaluate(G.SET_JS, ["input[name='octocaptcha-token']", G.OCTO_OK])
        v = pg.evaluate(G.VALIDITY_JS); rec['validity'] = v
        print('  valid=%s country=%r' % (v.get('valid'), v.get('country')), flush=True)
        if not v.get('country'):
            rec['result'] = 'COUNTRY_MISSING'; return rec
        for i in range(G.BUTTON_WAIT_S // 2):
            if pg.evaluate("() => {const b=document.querySelector('button.js-octocaptcha-form-submit');return b?b.disabled:null;}") is False: break
            time.sleep(2)
        pg.evaluate("() => document.querySelector('button.js-octocaptcha-form-submit').click()")
        time.sleep(12)
        rec['after_url'] = pg.url
        print('  after=%s' % pg.url[:80], flush=True)
        try: pg.screenshot(path=os.path.join(OUT, f'after{idx}.png'), full_page=True)
        except Exception: pass
        dump = {
            'url': pg.url, 'title': pg.title(),
            'text': pg.inner_text('body')[:2000],
            'inputs': pg.evaluate("""() => [...document.querySelectorAll('input,textarea,select')].map(e=>({
                tag:e.tagName,type:e.type,name:e.name,id:e.id,ph:e.placeholder,ac:e.autocomplete,
                ml:e.maxLength,vis:!!(e.offsetWidth||e.offsetHeight),val:(e.value||'').slice(0,10)}))"""),
            'buttons': pg.evaluate("""() => [...document.querySelectorAll('button,input[type=submit],a[role=button]')].map(e=>({
                txt:(e.innerText||e.value||'').trim().slice(0,40),id:e.id,cls:(e.className||'').slice(0,90),
                type:e.type,vis:!!(e.offsetWidth||e.offsetHeight)})).filter(x=>x.vis)"""),
            'forms': pg.evaluate("""() => [...document.querySelectorAll('form')].map(f=>({
                action:f.action,method:f.method,fields:[...f.elements].map(e=>e.name).filter(Boolean)}))"""),
        }
        rec['dom'] = dump
        json.dump(dump, open(os.path.join(OUT, f'dom{idx}.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
        print('  TEXT>>> ' + dump['text'][:400].replace('\n', ' | '), flush=True)
        print('  VISIBLE INPUTS>>> ' + json.dumps([x for x in dump['inputs'] if x['vis']], ensure_ascii=False)[:700], flush=True)
        print('  BUTTONS>>> ' + json.dumps(dump['buttons'], ensure_ascii=False)[:500], flush=True)
        print('  FORMS>>> ' + json.dumps(dump['forms'], ensure_ascii=False)[:400], flush=True)
        code, subj = G.wait_code(jwt, since=t_mail, timeout=150)
        rec['code'] = code; rec['subj'] = (subj or '')[:60]
        print('  code=%s subj=%s' % (code, rec['subj']), flush=True)
        rec['result'] = 'PROBED'
        return rec

def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    out = []
    for i in range(1, n + 1):
        try:
            out.append(probe(i, random.randint(1000, 99999)))
        except Exception:
            import traceback; tb = traceback.format_exc()
            print(tb, flush=True); out.append({'idx': i, 'result': 'EXC', 'trace': tb[-1200:]})
    json.dump({'probes': out}, open(os.path.join(HERE, 'otp_probe.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    print('\n===== SUMMARY =====', flush=True)
    for r in out:
        print('  %-16s %s %s' % (r.get('result'), r.get('email', '-'), r.get('after_url', '-')[:60]), flush=True)

if __name__ == '__main__':
    main()
