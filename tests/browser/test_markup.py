import pytest

pytestmark = pytest.mark.browser


@pytest.mark.parametrize(
    "markup",
    [
        "",
        "plain 'q' – US$",
        "a **b** c *d* e[^n1]",
        "Ёлка **x**[^a_b]",
        "a b  c",
        "2 * 3",
        "**",
        "<b>&amp;</b> \"q\"",
        "**a *b* c**",
        "x[^bad id]",
    ],
)
def test_round_trip_unchanged(js, markup):
    assert js.round_trip(markup, "inline") == {"ok": True, "markup": markup, "issues": []}


@pytest.mark.parametrize("markup", ["", "a **b** *c* & <d>", "x y"])
def test_round_trip_plain(js, markup):
    assert js.round_trip(markup, "plain") == {"ok": True, "markup": markup, "issues": []}


@pytest.mark.parametrize(
    "html,expected",
    [
        ("a&nbsp;b<br>", "a b"),
        ("<strong>a</strong><strong>b</strong>", "**ab**"),
        ("<b>x</b> <i>y</i>", "**x** *y*"),
        ("x<strong></strong>", "x"),
        ('<span class="de-fn" contenteditable="false" data-fn="n1">n1</span>', "[^n1]"),
        ("a &amp; &lt;b&gt;", "a & <b>"),
    ],
)
def test_normalises_browser_artefacts(js, html, expected):
    assert js.from_html(html, "inline", original="seed")["markup"] == expected


def test_nbsp_kept_when_original_has_it(js):
    assert js.from_html("a&nbsp;b!", "inline", original="x y")["markup"] == "a b!"


@pytest.mark.parametrize(
    "html",
    [
        "<div>a</div><div>b</div>",
        '<span style="color:red">a</span>',
        "<strong><em>x</em></strong>",
        "a<br>b",
        '<a href="x">l</a>',
        '<span class="de-fn" data-fn="bad id">x</span>',
    ],
)
def test_unrepresentable_goes_to_raw_editor(js, html):
    assert js.from_html(html, "inline", original="seed")["ok"] is False


def test_literal_star_flagged(js):
    result = js.from_html("2 * 3", "inline", original="seed")
    assert result["issues"] == ["A literal * will be read as emphasis"]


def test_plain_rejects_markup(js):
    assert js.from_html("<b>x</b>", "plain", original="x")["ok"] is False


def test_plain_text_edit(js):
    r = js.from_html("a&nbsp;b<br>", "plain", original="seed")
    assert r == {"ok": True, "markup": "a b", "issues": []}


def test_to_html_escapes(js):
    html = js.to_html('<i>"&"</i> **b** [^a"b]', "inline")
    assert html == "&lt;i&gt;&quot;&amp;&quot;&lt;/i&gt; <strong>b</strong> [^a&quot;b]"


def test_literal_star_issue(js):
    assert js.literal_star_issue("a **b** *c*") is None
    assert js.literal_star_issue("2 * 3") == "A literal * will be read as emphasis"
