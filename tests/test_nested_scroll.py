"""Nested-scroll regression. Local browser only; no model calls, no external sites."""

from urllib.parse import quote

import pytest

from jev_ultrafast.browser import Browser

NESTED = """<!doctype html><title>Nest</title><style>
body{margin:0;font:16px system-ui}header{padding:16px;height:28px}
#panel{height:600px;overflow-y:auto;border:1px solid #ccc;margin:0 16px}
#tall{padding:16px}</style>
<header>Nested scroll check</header>
<div id="panel"><div id="tall">
<label>Destination<input id="dest"></label>
""" + "<p>Filler line.</p>" * 60 + """
<button id="go" onclick="document.getElementById('done').textContent=
'Nested filters: Destination '+document.getElementById('dest').value+' applied'">Submit nested</button>
</div></div><p id="done"></p>"""

TALL = """<!doctype html><title>Tall</title><body style="margin:0">
<div style="height:2500px">Long page</div></body>"""


def need_browser():
    try:
        browser = Browser("about:blank")
    except Exception:
        pytest.skip("Local Chrome unavailable")
    return browser


def labels(page):
    return [(a.get("id"), a.get("kind"), a.get("label")) for a in page["actions"]]


def test_scroll_offered_while_submit_hidden():
    try:
        browser = Browser("data:text/html," + quote(NESTED))
    except Exception:
        pytest.skip("Local Chrome unavailable")
    try:
        page = browser.observe(screenshot=False)
        ids = {a["id"] for a in page["actions"]}
        assert "scroll_down" in ids
        assert not any("Submit nested" in (a.get("label") or "") for a in page["actions"])
    finally:
        browser.close()


def test_nested_scroll_reveals_and_clicks_submit():
    browser = Browser("data:text/html," + quote(NESTED))
    try:
        page = browser.observe(screenshot=False)
        field = next(a for a in page["actions"] if a.get("label") == "Destination")
        browser.act(field, page, text="Lisbon")
        for _ in range(5):
            page = browser.observe(screenshot=False)
            found = [a for a in page["actions"] if a.get("label") == "Submit nested"]
            if found:
                break
            browser.act({"id": "scroll_down", "kind": "scroll", "delta": 560}, page)
        else:
            raise AssertionError("Submit never entered the action space")
        submit = next(a for a in page["actions"] if a.get("label") == "Submit nested")
        browser.act(submit, page)
        assert "Lisbon" in browser.evaluate("document.getElementById('done').textContent")
    finally:
        browser.close()


def test_page_level_scroll_still_works():
    browser = Browser("data:text/html," + quote(TALL))
    try:
        page = browser.observe(screenshot=False)
        assert any(a["id"] == "scroll_down" for a in page["actions"])
        before = browser.evaluate("scrollY")
        browser.act({"id": "scroll_down", "kind": "scroll", "delta": 560}, page)
        assert browser.evaluate("scrollY") > before
    finally:
        browser.close()
