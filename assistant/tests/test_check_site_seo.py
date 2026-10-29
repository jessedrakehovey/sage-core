"""Offline tests for website/check_site.py --seo (2026-10-24): the on-page SEO pass over the sitemap.
Saved HTML fixtures (Squarespace-shaped) cover each flag: missing / duplicate / long title, missing /
duplicate meta description, 0 or 2 H1s, images without alt text (filename-as-alt counts, decorative
ones skipped, <noscript> repeats counted once), body links to the quiz / example page (menus
ignored), and internal links returning 4xx. No network.
   python assistant/tests/test_check_site_seo.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "website"))
import check_site as cs  # noqa: E402

S = "https://www.example.invalid"
NAV = '<header><nav><a href="/packagefinder">Check your date</a><a href="/luxury-wedding-videographer-santa-barbara">SB</a></nav></header>'
FOOT = '<footer><a href="/faq">FAQ</a></footer>'

HOME = f"""<html><head><title>example-org | example Wedding Videographer</title>
<meta name="description" content="Cinematic wedding films in example."></head><body>{NAV}
<h1>Wedding films</h1>
<img data-src="https://images.squarespace-cdn.com/content/v1/abc/Black.png?format=1500w" alt="Black.png">
<noscript><img src="https://images.squarespace-cdn.com/content/v1/abc/Black.png" alt="Black.png"></noscript>
<img data-src="https://images.squarespace-cdn.com/content/v1/abc/SBIFF+logo.png" alt="">
<img src="https://images.squarespace-cdn.com/content/v1/abc/kiss.jpg">
<img src="/divider.svg" alt="" role="presentation">
<img src="/couple.jpg" alt="Bride and groom kissing beneath her veil">
<p><a href="/packagefinder">Start the quiz</a> <a href="/films">Old films page</a>
<a href="https://instagram.com/example">IG</a> <a href="mailto:owner3@example.invalid">mail</a></p>
{FOOT}</body></html>"""

# duplicate title + duplicate description of /faq, no H1, long title note is on /about
FAQ = f"""<html><head><title>FAQ | example-org</title><meta name="description" content="Answers."></head>
<body>{NAV}<h2>FAQ</h2><p>No calls to action here.</p>{FOOT}</body></html>"""
FAQ_COPY = FAQ.replace("<body>", '<body><a href="https://example.invalid/luxury-wedding-videographer-santa-barbara#top">SB</a>')

ABOUT = f"""<html><head><title>About owner1 and owner6 example, example wedding videographers and filmmakers</title>
</head><body>{NAV}<h1>About</h1><h1>Us</h1>
<svg><title>icon</title></svg><a href="/luxury-wedding-videographer-santa-barbara?x=1">see</a>{FOOT}</body></html>"""

QUIZ = """<html><head></head><body><h1>Package finder</h1></body></html>"""

STATUS = {"/films": 404, "/faq": 200, "/packagefinder": 200, "/luxury-wedding-videographer-santa-barbara": 200}
calls = []


def link_status(path):
    calls.append(path)
    return STATUS.get(path, 200)


def test_sitemap_and_links():
    xml = (f'<urlset><url><loc>{S}/</loc><image:image><image:loc>{S}/x.jpg</image:loc></image:image></url>'
           f'<url><loc>{S}/faq?a=1&amp;b=2</loc></url></urlset>')
    assert cs.sitemap_urls(xml) == [f"{S}/", f"{S}/faq?a=1&b=2"]
    assert cs.internal_path("https://example.invalid/faq#q") == "/faq"
    assert cs.internal_path("/blog?offset=2") == "/blog"
    assert cs.internal_path("https://instagram.com/x") is None
    assert cs.internal_path("tel:805") is None and cs.internal_path("#top") is None


def test_audit():
    pages = {f"{S}/": HOME, f"{S}/faq": FAQ, f"{S}/faq-2": FAQ_COPY, f"{S}/about": ABOUT, f"{S}/packagefinder": QUIZ}
    rows = {r["page"]: r for r in cs.seo_audit(pages, link_status)}

    home = rows["/"]
    assert home["missing_alt"] == ["Black.png", "SBIFF+logo.png", "kiss.jpg"], home["missing_alt"]
    assert "3 image(s) without alt text" in home["flags"]
    assert home["links_quiz_or_sb"] and home["h1s"] == 1
    assert home["broken_links"] == ["/films (404)"] and "1 broken internal link(s)" in home["flags"]
    assert not any("duplicate" in f or "no " in f for f in home["flags"]), home["flags"]

    faq = rows["/faq"]
    assert "duplicate title" in faq["flags"] and "duplicate meta description" in faq["flags"]
    assert "no H1" in faq["flags"]
    assert not faq["links_quiz_or_sb"], "menu/footer links must not count as body links"
    assert any("no link to /packagefinder" in n for n in faq["notes"])
    assert rows["/faq-2"]["links_quiz_or_sb"], "absolute SB link with a fragment counts"

    about = rows["/about"]
    assert about["title"].startswith("About owner1"), "svg <title> must not replace the page title"
    assert any(n.startswith(f"title {about['title_len']} chars") for n in about["notes"])
    assert "2 H1s" in about["notes"] and "no meta description" in about["flags"]
    assert about["links_quiz_or_sb"], "query string on the SB link still counts"

    quiz = rows["/packagefinder"]
    assert "no title" in quiz["flags"] and "no meta description" in quiz["flags"]
    assert not any("no link" in n for n in quiz["notes"]), "the quiz page needn't link to itself"

    assert len(calls) == len(set(calls)), "each internal link is checked once across pages"


def test_main_exits_zero():
    sm = f"<urlset><url><loc>{S}/</loc></url></urlset>"
    real = cs.fetch_url
    cs.fetch_url = lambda url: (200, url, sm if url.endswith("sitemap.xml") else HOME)
    sys.argv = ["check_site.py", "--seo", "--json"]
    try:
        assert cs.main() == 0
    finally:
        cs.fetch_url, sys.argv = real, ["check_site.py"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
