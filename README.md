# Plain-English test runner (Python + Playwright + Claude)

## Setup
```bash
pip install -r requirements.txt
playwright install chromium
export ANTHROPIC_API_KEY=...        # optional: ANTHROPIC_MODEL, MAX_STEPS

# Live site testing
python qa_agent.py --url https://phptravels.com/demo --test "Log in as a customer and verify the dashboard is shown"
python qa_agent.py --url https://phptravels.com/demo --file examples/tests.txt --headed

# Offline / local fixture testing
python qa_agent.py --url examples/local_login.html --test "Read the demo credentials, fill email and password, click Login, and assert text Welcome to your account is shown" --headed
```

Output: PASS/FAIL per test in the console, `results/report.json` (steps + reason), a final screenshot. Exit code 0 only if every test passes.

## How it works
Loop (max 25 steps): (1) inject JS that tags every visible interactive element with a numeric id and returns URL, title and visible text;
(2) the LLM sees the test case, history and that snapshot and returns one JSON action (goto/click/fill/select/press/assert_*/finish);
(3) Playwright executes it. Nothing is site-specific: no selectors, URLs or flows in the code, so it works on any site.
**Verdicts are checked by code, not trusted from the LLM:** `assert_text/url/visible` are evaluated by Playwright; a failed assertion fails the test,
and `finish(pass)` is downgraded to FAIL unless at least one assertion actually passed.

## Limitations
- The page snapshot covers the main frame only (no iframes/shadow DOM); capped at 150 elements and 1500 characters of text.
- The LLM can pick a wrong element or loop; limited by MAX_STEPS. Results can vary between runs.
- CAPTCHAs, 2FA, file upload and drag-drop are not supported. Passwords in test text are sent to the LLM API.
- "Pass" is only as strong as the assertion the model chooses (e.g. the text "Dashboard"); review `report.json` for the evidence.
