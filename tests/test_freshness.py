"""Freshness and bounded revalidation contracts without Chrome or paid models."""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from jev_ultrafast import browser as executor
from jev_ultrafast.browser import Browser, StalePage


def observed():
    return {
        "title": "Issue", "page_key": ["document", "url"], "marker": ["Activity 0"],
        "guards": {"1": ["editor", "empty"]}, "scroll": {"y": 0, "nested": [[5, 0, 0]]},
        "actions": [{"id": "e1", "kind": "fill", "node": 1, "label": "Comment", "value": ""},
                    {"id": "scroll_down", "kind": "scroll", "delta": 560}],
    }


@pytest.mark.parametrize("changed", [False, True])
def test_fill_freshness_uses_guard_not_page_marker(changed):
    page = observed()
    browser = Browser.__new__(Browser)
    guard = ["editor", "changed"] if changed else page["guards"]["1"]
    browser.evaluate = Mock(return_value=[page["page_key"], guard])
    page["marker"] = ["unrelated text drift"]
    assert browser.fresh(page, page["actions"][0]) is not changed
    assert "c.guard" in browser.evaluate.call_args.args[0]


@pytest.mark.parametrize("mutation", ["text", "document", "guard", "actions", "nested-scroll", "title"])
def test_revalidation_only_accepts_unrelated_text(mutation):
    page = observed()
    current = deepcopy(page)
    current["marker"] = ["Activity 1"]
    if mutation == "document":
        current["page_key"] = ["new-document", "url"]
    elif mutation == "guard":
        current["guards"]["1"] = ["editor", "changed"]
    elif mutation == "actions":
        current["actions"][0]["kind"] = "click"
    elif mutation == "nested-scroll":
        current["scroll"]["nested"][0][1] = 560
    elif mutation == "title":
        current["title"] = "Different issue"
    browser = Browser.__new__(Browser)
    browser.observe = Mock(return_value=current)
    browser.fresh = Mock(return_value=True)
    if mutation == "text":
        assert browser.revalidate(page, page["actions"][1]) is current
    else:
        with pytest.raises(StalePage):
            browser.revalidate(page, page["actions"][1])
        browser.fresh.assert_not_called()


def test_revalidation_is_bounded_before_any_mutation(monkeypatch):
    page = observed()
    browser = Browser.__new__(Browser)
    browser.observe = Mock(return_value=page)
    browser.fresh = Mock(return_value=False)
    operation = Mock()
    monkeypatch.setattr(executor, "browser_operation", operation)
    with pytest.raises(StalePage):
        browser.act(page["actions"][1], page)
    assert browser.observe.call_count == 2
    operation.assert_not_called()


def test_scroll_revalidates_without_repeating_browser_input(monkeypatch):
    page = observed()
    browser = Browser.__new__(Browser)
    browser.session = "offline"
    browser.observe = Mock(return_value=deepcopy(page))
    browser.fresh = Mock(side_effect=[False, True])
    operation = Mock(return_value={"executed": "scroll_down"})
    monkeypatch.setattr(executor, "browser_operation", operation)
    assert browser.act(page["actions"][1], page)["revalidated"]
    operation.assert_called_once()
