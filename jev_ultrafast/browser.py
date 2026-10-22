"""Observed actions through Browser Harness; one CDP session, no per-step subprocess."""

import hashlib
import json
import sys
import time
from pathlib import Path

from browser_harness.admin import ensure_daemon
from browser_harness.helpers import cdp

# Atomically read visible content and controls, preserving actual DOM node identity.
READ_STATE = Path(__file__).with_name("snapshot.js").read_text()
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"

class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


class Browser:
    def __init__(self, url):
        ensure_daemon()
        self.target = cdp("Target.createTarget", url="about:blank", background=True)["targetId"]
        self.session = cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
        self.call("Emulation.setDeviceMetricsOverride", width=1120, height=780, deviceScaleFactor=1, mobile=False)
        # Keep rAF/menus rendering in an owned background tab, without activating the user's Chrome tab.
        self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        self.call("Page.navigate", url=url)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.evaluate("document.readyState") == "complete":
                break
            time.sleep(0.02)

    def call(self, method, **params):
        return cdp(method, session_id=self.session, **params)

    def evaluate(self, expression):
        response = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    def observe(self, screenshot=True):
        if getattr(self, "after_input", None):
            action, self.after_input = self.after_input, None
            # This is read-only and happens after execution was logged, even if navigation interrupts it.
            try:
                self.call(
                    "Runtime.evaluate",
                    expression="""(action => new Promise(resolve => {
                      const field=window.__jevFast?.nodes.get(action.node);
                      const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
                      let frames=0, stopped=false;
                      const finish=()=>{stopped=true;resolve()};
                      setTimeout(finish,action.kind==='click' ? 1000 : autocomplete ? 200 : 50);
                      const ready=()=>{
                        if (stopped) return;
                        const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
                          .split(/\\s+/).filter(Boolean);
                        const roots=ids.length ? ids.map(id=>document.getElementById(id)).filter(Boolean) : [document];
                        const options=roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]);
                        const opening=action.kind==='click' && field &&
                          (!field.isConnected || !field.checkVisibility({checkVisibilityCSS:true}));
                        const active=document.activeElement;
                        const editorReady=active &&
                          (active.isContentEditable || active.matches('input,textarea,select')) &&
                          active.checkVisibility({checkVisibilityCSS:true}) && window.__jevFast?.point(active);
                        if (++frames>=2 && (!opening || editorReady) && (!autocomplete || options.some(e=>{
                          const r=e.getBoundingClientRect();
                          return r.width && r.height && r.bottom>0 && r.top<innerHeight &&
                            e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
                        }))) finish();
                        else requestAnimationFrame(ready);
                      };
                      requestAnimationFrame(ready);
                    }))(""" + json.dumps(action) + ")",
                    awaitPromise=True,
                    returnByValue=True,
                )
            except RuntimeError:
                pass
        for attempt in range(10):
            try:
                return browser_operation(
                    {"operation": "observe", "session": self.session, "screenshot": screenshot}
                )
            except StalePage:
                if attempt == 9:
                    raise
                time.sleep(0.02)
        raise StalePage("Page did not settle")

    def fresh(self, page, action=None):
        if action is not None and action["kind"] in {"click", "select", "fill"}:
            node = action["node"]
            if type(node) is not int:
                return False
            current = self.evaluate(
                "(() => { const c=window.__jevFast; "
                f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()"
            )
            return current == [page["page_key"], page["guards"].get(str(node))]
        return self.evaluate(MARKER) == page["marker"]

    def revalidate(self, page, action):
        # Only text drift may reuse a nonterminal decision; never remap a changed action space.
        def semantics(actions):
            return [{k: v for k, v in a.items() if k != "rect"} for a in actions]

        for _ in range(2):
            current = self.observe(screenshot=False)
            if (current["page_key"] != page["page_key"] or current["title"] != page["title"]
                    or current["scroll"] != page["scroll"] or current["guards"] != page["guards"]
                    or semantics(current["actions"]) != semantics(page["actions"])):
                break
            if self.fresh(current, action):
                return current
        raise StalePage("Page or action space changed since this decision. Observe again.")

    def act(self, action, page, text=None):
        revalidated = False
        if not self.fresh(page, action):
            if action["kind"] not in {"scroll", "wait"}:
                raise StalePage("Page changed since this decision. Observe again.")
            page = self.revalidate(page, action)
            revalidated = True
        if action["kind"] == "wait":
            time.sleep(0.1)
        result = browser_operation({"operation": "act", "session": self.session, "action": action, "text": text})
        self.after_input = action if action["kind"] != "wait" else None
        return {**result, "revalidated": revalidated}

    def close(self):
        if self.target:
            cdp("Target.closeTarget", targetId=self.target)
            self.target = None


def fingerprint(state):
    content = {k: state[k] for k in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def browser_operation(request):
    operation = request["operation"]
    session = request["session"]

    def call(method, **params):
        return cdp(method, session_id=session, **params)

    def evaluate(expression):
        result = call("Runtime.evaluate", expression=expression, returnByValue=True)
        if result.get("exceptionDetails"):
            if operation == "act" and request["action"]["kind"] == "select":
                raise RuntimeError("Dropdown execution was interrupted; inspect before retrying.")
            raise StalePage("Document changed during evaluation")
        return result.get("result", {}).get("value")

    if operation == "act":
        action = request["action"]
        kind = action["kind"]
        if kind == "scroll":
            # Wheel events do not move owned background tabs, so scroll explicitly:
            # the window when the page can move that way, else the largest visible
            # nested container that can. The wheel dispatch stays as a last resort.
            state = evaluate(
                "(delta => {"
                " const pageCan=delta>0"
                " ? scrollY+innerHeight<document.documentElement.scrollHeight-2"
                " : scrollY>2;"
                " if(pageCan){ scrollBy(0,delta); return {pageCan:true,nested:false}; }"
                " const els=[...document.querySelectorAll("
                " 'div,section,main,article,aside,form')];"
                " els.sort((a,b)=>(b.clientWidth*b.clientHeight)"
                "  -(a.clientWidth*a.clientHeight));"
                " for(const e of els){"
                "  try{"
                "   if(!(e instanceof HTMLElement)"
                "    ||e.scrollHeight<=e.clientHeight+10) continue;"
                "   if(!/(auto|scroll)/.test(getComputedStyle(e).overflowY)) continue;"
                "   const r=e.getBoundingClientRect();"
                "   if(r.width<=0||r.height<=0||r.bottom<=0||r.top>=innerHeight"
                "     ||r.right<=0||r.left>=innerWidth) continue;"
                "   if(delta>0&&e.scrollTop+e.clientHeight>=e.scrollHeight-2) continue;"
                "   if(delta<0&&e.scrollTop<=2) continue;"
                "   e.scrollTop+=delta; return {pageCan:false,nested:true};"
                "  }catch(_){}"
                " }"
                " return {pageCan:false,nested:false};"
                "})(" + json.dumps(action["delta"]) + ")"
            )
            if not state["pageCan"] and not state["nested"]:
                call("Input.dispatchMouseEvent", type="mouseWheel",
                     x=550, y=650, deltaX=0, deltaY=action["delta"])
        elif kind != "wait":
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            # Code-owned node IDs refer to actual observed elements, never model-generated selectors.
            target = evaluate("""(action => {
              const e=window.__jevFast?.nodes.get(action.node);
              if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
                  !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
              if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true' ||
                  !(e.isContentEditable || ['INPUT','TEXTAREA'].includes(e.tagName)))) return null;
              const point=window.__jevFast.point(e);
              if (!point) return null;
              if (action.kind==='select') {
                if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
                    !o.disabled && !o.closest('optgroup[disabled]'))) return null;
                e.value=action.value;
                e.dispatchEvent(new Event('input',{bubbles:true}));
                e.dispatchEvent(new Event('change',{bubbles:true}));
              }
              return point;
            })(""" + json.dumps(action) + ")")
            if target is None:
                if kind == "select":
                    raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
                raise StalePage("Target changed or is covered. Observe again.")
            if kind != "select":
                x, y = target["x"], target["y"]
                for event in ("mousePressed", "mouseReleased"):
                    call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
                if kind == "fill":
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyDown",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                        commands=["selectAll"],
                    )
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyUp",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                    )
                    call("Input.insertText", text=request["text"])
        return {"executed": action["id"]}

    info = evaluate(READ_STATE)
    if info is None:
        raise StalePage("Document is navigating")
    info["fingerprint"] = fingerprint(info)
    if request.get("screenshot", True):
        info["screenshot"] = call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
    return info
