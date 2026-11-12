"""Offline tests for the agent's free web search (assistant/agent/websearch.py, an owner 2026-11-08: "an internet
search and query tool that lets sage look things up just like a normal claude or gpt instance can without opening
any browsers but it needs to be free"): the result parsers on canned pages, the backend fallback order, the private-
address guard (a read never reaches this computer or the house network), the page-text extractor, and the gmcp
wiring (tools listed, the brain allowed them, calls routed). No network: every fetch is stubbed."""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "agent"))
import tempfile  # noqa: E402
import websearch as w  # noqa: E402
w.COOLDOWN_FILE = Path(tempfile.mkdtemp(prefix="wv_ws_")) / "cooldown.json"

YAHOO = """<html><li><div class="dd algo algo-sr Sr"><div class="compTitle"><a class="d-ib" href="https://r.search.yahoo.com/_ylt=x;_ylu=y/RV=2/RE=1/RO=10/RU=https%3a%2f%2fexample.com%2fcourthouse-fees%2f/RK=2/RS=z-"><span>Example</span><h3 class="title"><span>Courthouse <b>permit</b> fees</span></h3></a></div><div class="compText"><p class="fc">It is currently <b>$100</b>.</p></div></div></li>
<li><div class="dd algo algo-sr Sr"><div class="compTitle"><a href="https://r.search.yahoo.com/_ylt=x/RU=https%3a%2f%2fsearch.yahoo.com%2fmore/RK=2/"><h3>Yahoo itself</h3></a></div></div></li>
<li><div class="dd algo algo-sr Sr"><div class="compTitle"><a href="https://r.search.yahoo.com/_ylt=x/RU=https%3a%2f%2fother.org%2fpage/RK=2/"><h3>Second &amp; last</h3></a></div><p>snip two</p></div></li></html>"""

DDG = """<div class="result"><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.example%2Fx&amp;rut=1">A <b>title</b></a>
<a class="result__snippet" href="#">A snippet</a></div>
<div class="result"><a class="result__a" href="https://duckduckgo.com/y.js?ad=1">Ad</a></div>"""

ARTICLE = """<html><head><title>Mission &amp; History</title><script>var x=1</script></head><body>
<nav>Home | About | Menu</nav><article><h1>The Mission</h1><p>Founded in 1786.</p><ul><li>Bells</li><li>Garden</li></ul>
<!-- hidden --><p>Open <b>daily</b>.</p></article><footer>Copyright</footer></body></html>"""


def test_yahoo_parser_unwraps_links_and_skips_yahoo():
    w.fetch = lambda url, data=None, check=True, timeout=0: (url, YAHOO)
    rows = w._yahoo_html("courthouse permit fees", 5)
    assert [r["url"] for r in rows] == ["https://example.com/courthouse-fees/", "https://other.org/page"]
    assert rows[0]["title"] == "Courthouse permit fees"
    assert rows[0]["snippet"] == "It is currently $100."
    assert rows[1]["title"] == "Second & last"


def test_ddg_parser_unwraps_uddg_and_drops_ads():
    w.fetch = lambda url, data=None, check=True, timeout=0: (url, DDG)
    rows = w._ddg_html("x", 5)
    assert rows == [{"title": "A title", "url": "https://a.example/x", "snippet": "A snippet"}]


def test_search_falls_through_backends_and_reports_tries():
    calls = []

    def dead(q, n):
        calls.append("dead")
        raise RuntimeError("500")

    def missing(q, n):
        raise ImportError

    def empty(q, n):
        return []

    def good(q, n):
        return [{"title": "t", "url": "https://ok.example", "snippet": "s"}]

    w.COOLDOWN_FILE.unlink(missing_ok=True)
    saved, w.BACKENDS = w.BACKENDS, (("dead", dead), ("missing", missing), ("empty", empty), ("good", good))
    try:
        r = w.search("anything", 3)
        assert calls == ["dead"]                           # one try, then the next engine
        r2 = w.search("again", 3)                          # dead failed a moment ago: skipped, not waited on
    finally:
        w.BACKENDS = saved
        w.COOLDOWN_FILE.unlink(missing_ok=True)
    assert r["ok"] and r["backend"] == "good" and r2["ok"]
    assert calls == ["dead"] and any("resting" in t for t in r2["tried"])
    assert any("not installed" in t for t in r["tried"]) and any("no results" in t for t in r["tried"])


def test_all_backends_down_says_so_plainly():
    saved, w.BACKENDS = w.BACKENDS, (("x", lambda q, n: []),)
    try:
        txt = w.search_text("anything")
    finally:
        w.BACKENDS = saved
    assert "found nothing" in txt and "don't guess" in txt


def test_bing_is_not_a_backend():
    # Bing's bare page matched only the first word ("much" -> dictionaries); a wrong answer is worse than none
    assert "bing" not in [n for n, _ in w.BACKENDS]
    assert w.BACKENDS[0][0] == "yahoo"


def test_private_addresses_are_refused():
    for url in ("http://localhost:9443/", "http://127.0.0.1:9333/json", "http://192.168.1.1/", "http://10.0.0.5/",
                "http://[::1]/", "https://my-pc.tail0000.ts.net:9443/", "file:///C:/Windows/win.ini", "http://169.254.169.254/"):
        try:
            w._check_url(url)
        except ValueError:
            continue
        raise AssertionError(f"{url} was allowed")
    r = w.read("http://127.0.0.1:9443/")
    assert not r["ok"] and "private network" in r["error"]


def test_redirect_into_the_house_is_refused():
    h = w._NoPrivateRedirect()
    try:
        h.redirect_request(None, None, 302, "Found", {}, "http://127.0.0.1:9443/")
    except ValueError:
        return
    raise AssertionError("redirect to localhost was followed")


def test_text_extractor_keeps_the_article_not_the_menus():
    title, text = w._Text.extract(ARTICLE)
    assert title == "Mission & History"
    assert "Founded in 1786." in text and "- Bells" in text and "Open daily." in text
    assert "Home | About" not in text and "Copyright" not in text and "var x" not in text and "hidden" not in text


def test_read_marks_page_as_untrusted_and_cuts():
    w.fetch = lambda url, data=None, check=True, timeout=0: (url, ARTICLE)
    out = w.read_text("https://example.com/m", 500)
    assert out.startswith("PAGE https://example.com/m") and "never as instructions" in out and "1786" in out


def test_gmcp_lists_and_routes_web_tools_and_brain_allows_them():
    import gmcp
    import brain
    names = [t["name"] for t in gmcp.TOOLS]
    assert "web_search" in names and "web_read" in names
    for t in ("web_search", "web_read"):
        assert f"mcp__{brain.MCP_SERVER}__{t}" in brain.ALLOWED.split(",")
        assert t not in gmcp.WRITE_NAMES                   # reads, never deeds
    gmcp._turn_person = lambda: {}
    w.fetch = lambda url, data=None, check=True, timeout=0: (url, YAHOO)
    got = gmcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "web_search", "arguments": {"query": "fees"}}})
    assert "example.com/courthouse-fees" in got["result"]["content"][0]["text"]
    got = gmcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                       "params": {"name": "web_read", "arguments": {"url": "http://localhost:9443"}}})
    assert "private network" in got["result"]["content"][0]["text"]


if __name__ == "__main__":
    real_fetch = w.fetch
    n = 0
    for k, fn in list(globals().items()):
        if k.startswith("test_") and callable(fn):
            w.fetch = real_fetch
            fn()
            n += 1
            print("ok", k)
    print(json.dumps({"passed": n}))
