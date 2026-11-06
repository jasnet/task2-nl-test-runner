#!/usr/bin/env python3
"""Plain-English test runner: an LLM reads the live page and picks actions; Python executes and verifies them.
Usage: python qa_agent.py --url https://phptravels.com/demo --test "Log in as customer and verify the dashboard is shown"
       python qa_agent.py --file examples/tests.txt
"""
import argparse, json, os, re, sys, time
from pathlib import Path
from anthropic import Anthropic
from playwright.sync_api import sync_playwright

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022")
MAX_STEPS = int(os.getenv("MAX_STEPS", "25"))

SNAPSHOT_JS = """() => {
  document.querySelectorAll('[data-qa-id]').forEach(e => e.removeAttribute('data-qa-id'));
  const sel = 'a,button,input,select,textarea,[role=button],[role=link],[role=tab],[role=menuitem],[role=checkbox],[role=radio],[onclick]';
  const out = []; let id = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    if (!(r.width && r.height) || cs.visibility === 'hidden' || cs.display === 'none') continue;
    el.setAttribute('data-qa-id', ++id);
    const lab = (el.labels && el.labels[0] && el.labels[0].innerText) || el.getAttribute('aria-label') || '';
    out.push({ id, tag: el.tagName.toLowerCase(), type: el.type || '', text: (el.innerText || '').trim().slice(0, 80),
      label: lab.trim().slice(0, 60), placeholder: el.placeholder || '', name: el.name || '',
      href: (el.getAttribute('href') || '').slice(0, 80), value: el.type === 'password' ? '' : (el.value || '').slice(0, 40),
      checked: !!el.checked, options: el.tagName === 'SELECT' ? [...el.options].slice(0, 12).map(o => o.text.trim()) : undefined });
    if (id >= 150) break;
  }
  return { url: location.href, title: document.title, text: document.body.innerText.replace(/\\s+/g, ' ').slice(0, 1500), elements: out };
}"""

SYSTEM = """You are a QA automation agent controlling a web browser to execute ONE test case written in plain English.
Each turn you get the current page (URL, visible text, numbered interactive elements) and the history of your actions.
Reply with ONE JSON object only, no prose, no markdown:
{"thought": "short reason", "action": "...", "id": <element id>, "value": "<text>"}
Actions:
 goto(value=url) | click(id) | fill(id,value) | select(id,value=option text) | press(value=key, e.g. Enter) | wait(value=seconds)
 assert_text(value=text that must be visible on page) | assert_url(value=substring) | assert_visible(id)
 finish(value=pass|fail|blocked, thought=reason)
Rules:
- Use only credentials/data given in the test case or visible on the page (demo sites often print demo credentials on the page).
- Do not hard-code assumptions about the site; read the page. Dismiss cookie banners/popups if they block you.
- To prove an expectation (e.g. "dashboard is shown") you MUST run an assert_* action on real evidence from the page BEFORE finish(pass).
- If an assertion fails, the test fails. If you cannot proceed (element missing, site error) finish with blocked or fail and say why.
"""

def parse_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m: raise ValueError("no JSON in model reply: " + text[:200])
    return json.loads(m.group(0))

def run_test(page_ctx, client, url, test, outdir, headless_log=print):
    page = page_ctx.new_page(); state = {"page": page}
    page_ctx.on("page", lambda p: state.update(page=p))  # follow new tabs
    history, passed_asserts, verdict, reason = [], 0, "fail", "no verdict"
    if url:
        if not url.startswith(("http://", "https://", "file://")) and Path(url).exists():
            url = Path(url).resolve().as_uri()
        state["page"].goto(url, wait_until="domcontentloaded")
    for step in range(1, MAX_STEPS + 1):
        pg = state["page"]
        try: pg.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception: pass
        snap = pg.evaluate(SNAPSHOT_JS)
        msg = f"TEST CASE: {test}\n\nHISTORY:\n" + "\n".join(history[-12:]) + \
              f"\n\nCURRENT PAGE:\n{json.dumps(snap)}\n\nNext action (JSON):"
        reply = client.messages.create(model=MODEL, max_tokens=400, system=SYSTEM,
                                       messages=[{"role": "user", "content": msg}]).content[0].text
        try: act = parse_json(reply)
        except Exception as e: history.append(f"step {step}: bad JSON ({e})"); continue
        a, i, v = act.get("action"), act.get("id"), str(act.get("value", ""))
        headless_log(f"  step {step}: {a} id={i} value={v!r} - {act.get('thought','')}")
        loc = pg.locator(f'[data-qa-id="{i}"]') if i is not None else None
        try:
            if a == "goto": pg.goto(v, wait_until="domcontentloaded")
            elif a == "click": loc.click(timeout=5000)
            elif a == "fill": loc.fill(v, timeout=5000)
            elif a == "select": loc.select_option(label=v, timeout=5000)
            elif a == "press": pg.keyboard.press(v)
            elif a == "wait": time.sleep(min(float(v or 1), 10))
            elif a == "assert_text":
                pg.wait_for_function("t => document.body.innerText.toLowerCase().includes(t.toLowerCase())", arg=v, timeout=6000)
                passed_asserts += 1
            elif a == "assert_url":
                pg.wait_for_function("t => location.href.includes(t)", arg=v, timeout=6000); passed_asserts += 1
            elif a == "assert_visible": loc.wait_for(state="visible", timeout=6000); passed_asserts += 1
            elif a == "finish":
                verdict, reason = v.lower(), act.get("thought", "")
                if verdict == "pass" and passed_asserts == 0:
                    verdict, reason = "fail", "model claimed pass without any verified assertion"
                break
            else: history.append(f"step {step}: unknown action {a}"); continue
            history.append(f"step {step}: {a} id={i} value={v!r} -> ok")
            time.sleep(1)
        except Exception as e:
            err = str(e).splitlines()[0][:160]
            history.append(f"step {step}: {a} id={i} value={v!r} -> ERROR {err}")
            if a.startswith("assert"):
                verdict, reason = "fail", f"assertion failed: {a} {v!r} ({err})"; break
    else:
        verdict, reason = "fail", f"no verdict within {MAX_STEPS} steps"
    shot = outdir / f"result_{int(time.time())}.png"
    try: state["page"].screenshot(path=str(shot))
    except Exception: pass
    return {"test": test, "verdict": verdict.upper(), "reason": reason, "steps": history, "screenshot": str(shot)}

class DemoClient:
    class messages:
        @staticmethod
        def create(model, max_tokens, system, messages):
            user_msg = messages[0]["content"]
            m = re.search(r'CURRENT PAGE:\n(.*?)\n\nNext action', user_msg, re.S)
            elements = []
            if m:
                try: elements = json.loads(m.group(1)).get("elements", [])
                except Exception: pass
            hm = re.search(r'HISTORY:\n(.*?)\n\nCURRENT PAGE', user_msg, re.S)
            history = hm.group(1) if hm else ""
            email_id = next((e["id"] for e in elements if "email" in (e.get("name","")+e.get("type","")+e.get("label","")+e.get("placeholder","")).lower()), None)
            pass_id = next((e["id"] for e in elements if "password" in (e.get("name","")+e.get("type","")+e.get("label","")+e.get("placeholder","")).lower()), None)
            btn_id = next((e["id"] for e in elements if e.get("tag") in ("button","input") and e.get("type") in ("submit","button","")), None)
            steps = len([l for l in history.splitlines() if "->" in l])
            if steps == 0 and email_id:
                reply = json.dumps({"thought": "Fill customer demo email", "action": "fill", "id": email_id, "value": "user@phptravels.com"})
            elif steps == 1 and pass_id:
                reply = json.dumps({"thought": "Fill customer demo password", "action": "fill", "id": pass_id, "value": "demopass"})
            elif steps == 2 and btn_id:
                reply = json.dumps({"thought": "Submit login form", "action": "click", "id": btn_id, "value": ""})
            elif steps == 3:
                reply = json.dumps({"thought": "Verify dashboard welcome message", "action": "assert_text", "id": None, "value": "Welcome"})
            else:
                reply = json.dumps({"thought": "Dashboard assertion verified successfully", "action": "finish", "id": None, "value": "pass"})
            return type("Reply", (), {"content": [type("Content", (), {"text": reply})]})()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="start URL (optional if the test case names the site)")
    ap.add_argument("--test", action="append", help="plain-English test case (repeatable)")
    ap.add_argument("--file", help="text file, one test case per line")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--demo", action="store_true", help="run in demo mode without Anthropic API key")
    a = ap.parse_args()
    tests = (a.test or []) + ([l.strip() for l in Path(a.file).read_text().splitlines() if l.strip() and not l.startswith("#")] if a.file else [])
    if not tests: ap.error("give --test or --file")
    outdir = Path("results"); outdir.mkdir(exist_ok=True)
    if a.demo:
        client = DemoClient()
    elif not os.getenv("ANTHROPIC_API_KEY"):
        sys.exit("Error: ANTHROPIC_API_KEY environment variable is not set.\nPlease set it via: export ANTHROPIC_API_KEY='your-key'\n(Or run with --demo for offline demo mode)")
    else:
        client = Anthropic()
    results = []
    (outdir / "videos").mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not a.headed)
        for t in tests:
            print(f"\nTEST: {t}")
            ctx = browser.new_context(viewport={"width": 1366, "height": 900}, record_video_dir=str(outdir / "videos"))
            r = run_test(ctx, client, a.url, t, outdir); ctx.close(); results.append(r)
            print(f"RESULT: {r['verdict']} - {r['reason']}")
        browser.close()
    (outdir / "report.json").write_text(json.dumps(results, indent=2))
    sys.exit(0 if all(r["verdict"] == "PASS" for r in results) else 1)

if __name__ == "__main__":
    main()
