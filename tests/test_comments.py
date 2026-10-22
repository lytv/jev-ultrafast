"""Real local comment workflow with deterministic page churn and offline model doubles."""

from pathlib import Path

import pytest

from jev_ultrafast import Agent
from jev_ultrafast import agent as loop
from jev_ultrafast.browser import StalePage

COMMENT = "Synthetic booking SYN-1: scheduled 10:00, displayed 09:00."
FIXTURE = Path(__file__).with_name("fixtures") / "comments.html"


def pick(choice, operation):
    return {
        "choice": choice, "operation": operation, "target": None,
        "confidence": 1.0, "probabilities": {choice: 1.0}, "latency_ms": 0, "usage": {},
    }


@pytest.mark.parametrize("changing", [False, True], ids=["stable", "changing"])
def test_comment_workflow(monkeypatch, changing):
    monkeypatch.setattr(loop, "MAX_STEPS", 8)
    with Agent(FIXTURE.as_uri(), f'Post exactly one comment: "{COMMENT}"') as agent:
        def churn():
            agent.browser.evaluate("document.getElementById('activity').textContent += '.'")

        def choose(page, goal, history):
            # Change only unrelated visible text during each nonterminal model request.
            # The terminal request still requires full-page stillness.
            if changing and not any(h["action"] == "Save" for h in history):
                churn()
            if any(h["action"] == "Save" for h in history):
                return pick("DONE", "DONE")
            fields = [a for a in page["actions"] if a["kind"] == "fill" and a["label"] == "Comment"]
            if fields and not fields[0]["value"]:
                return pick(fields[0]["id"], "TYPE_TEXT")
            for label in ("Save", "Add a comment"):
                action = next((a for a in page["actions"] if a["label"] == label and a["kind"] == "click"), None)
                if action:
                    return pick(action["id"], "CLICK")
            return pick("scroll_down", "SCROLL_DOWN")

        def field_text(context):
            assert context["field"]["label"] == "Comment", "Never type the comment into global Search"
            if changing:
                churn()  # Also exercise the race after generation, before browser input.
            return COMMENT, {"model": "offline", "latency_ms": 0}

        monkeypatch.setattr(loop, "choose", choose)
        monkeypatch.setattr(loop, "field_text", field_text)
        list(agent.run())
        assert agent.state["status"] == "done"
        assert len(agent.state["decisions"]) <= 8
        assert len(agent.state["text_calls"]) == 1
        assert [h["kind"] for h in agent.state["history"] if h["kind"] != "scroll"] == ["click", "fill", "click"]
        assert agent.state["history"][0]["kind"] == "scroll"
        # Independent DOM verification, not the stub's DONE assertion.
        comments = agent.browser.evaluate("[...document.querySelectorAll('#comments li')].map(e=>e.textContent)")
        assert comments == [COMMENT]
        assert not agent.state["stale_discards"]
        if changing:
            scrolls = sum(h["kind"] == "scroll" for h in agent.state["history"])
            assert agent.state["stale_revalidations"]["pre_input"] == scrolls


@pytest.mark.parametrize("fully_covered", [False, True], ids=["partly-covered", "fully-covered"])
def test_placeholder_hit_test_uses_only_exposed_points(fully_covered):
    with Agent(FIXTURE.as_uri(), "Post a synthetic comment") as agent:
        browser = agent.browser
        browser.evaluate("document.getElementById('panel').scrollTop = 900")
        page = browser.observe(screenshot=False)
        assert any(a["label"] == "Search" and a["kind"] == "fill" for a in page["actions"])
        assert any(a["label"] == "Reply" for a in page["actions"])
        placeholder = next(a for a in page["actions"] if a["label"] == "Add a comment")
        assert browser.evaluate("""(() => {
          const e=document.getElementById('placeholder'), r=e.getBoundingClientRect();
          return e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));
        })()""") is False
        if fully_covered:
            browser.evaluate("Object.assign(document.getElementById('quick').style,{top:'0',height:'100%'})")
            with pytest.raises(StalePage):
                browser.act(placeholder, page)
            assert not any(a["label"] == "Add a comment" for a in browser.observe(False)["actions"])
            assert browser.evaluate("!!document.getElementById('editor')") is False
        else:
            browser.act(placeholder, page)
            browser.observe(False)
            assert browser.evaluate("!!document.getElementById('editor')") is True


@pytest.mark.parametrize("change_target", [False, True], ids=["marker-drift", "target-change"])
@pytest.mark.parametrize("after_helper", [False, True], ids=["before-helper", "after-helper"])
def test_queued_fill_checks_target_guard(monkeypatch, change_target, after_helper):
    with Agent(FIXTURE.as_uri(), "Enter a synthetic comment") as agent:
        browser = agent.browser
        browser.evaluate("document.getElementById('panel').scrollTop = 900")
        page = browser.observe(screenshot=False)
        placeholder = next(a for a in page["actions"] if a["label"] == "Add a comment" and a["kind"] == "click")
        browser.act(placeholder, page)
        browser.observe(screenshot=False)
        # Expansion can put the editor below the nested viewport's clipping edge.
        browser.evaluate("document.getElementById('panel').scrollTop = 900")
        page = browser.observe(screenshot=False)
        action = next(a for a in page["actions"] if a["kind"] == "fill" and a["label"] == "Comment")
        agent.state["page"] = page
        monkeypatch.setattr(loop, "choose", lambda *_: pick(action["id"], "TYPE_TEXT"))
        helper_calls = []

        def drift():
            if change_target:
                browser.evaluate("document.getElementById('editor').setAttribute('aria-label','Different field')")
            else:
                browser.evaluate("document.getElementById('activity').textContent += '.'")

        def field_text(context):
            helper_calls.append(context)
            if after_helper:
                drift()
            return COMMENT, {"model": "offline", "latency_ms": 0}

        monkeypatch.setattr(loop, "field_text", field_text)
        agent.command("predict")
        if not after_helper:
            drift()
        if change_target:
            with pytest.raises(StalePage):
                agent.command("act", {"fingerprint": page["fingerprint"]})
            assert len(helper_calls) == int(after_helper)
            assert agent.state["stale_discards"] == {"pre_input" if after_helper else "pre_text": 1}
        else:
            agent.command("act", {"fingerprint": page["fingerprint"]})
            assert len(helper_calls) == 1
        assert browser.evaluate("document.getElementById('editor').innerText") == ("" if change_target else COMMENT)
        assert agent.state["decision"] is None


@pytest.mark.parametrize("editable", [False, True], ids=["textbox-placeholder", "empty-editable-attribute"])
def test_textbox_operations_require_actual_editability(editable):
    with Agent(FIXTURE.as_uri(), "Observe a synthetic editor") as agent:
        browser = agent.browser
        browser.evaluate("""(editable => {
          const e=document.createElement('div');
          e.id='placeholder'; e.setAttribute('role','textbox'); e.textContent='Add a comment';
          if (editable) e.setAttribute('contenteditable','');
          else e.tabIndex=0;
          document.getElementById('placeholder').replaceWith(e);
          document.getElementById('panel').scrollTop=900;
        })(""" + str(editable).lower() + ")")
        actions = [a for a in browser.observe(False)["actions"] if a["label"] == "Add a comment"]
        assert len(actions) == 1
        assert actions[0]["kind"] == ("fill" if editable else "click")
