"""The one-line excerpt an automation card leads with."""

import pytest

from src.server.services.automation_excerpt import plain_excerpt


@pytest.mark.parametrize(
    "answer, excerpt",
    [
        ("## Summary\n\nStocks **rose** today.", "Summary Stocks rose today."),
        ("Intro:\n\n- one\n- two\n\n> quoted", "Intro: one two quoted"),
        ("| a | b |\n|---|:---:|\n| 1 | 2 |", "a b 1 2"),
        ("See [the filing](https://example.com) now.", "See the filing now."),
        ("Before\n```python\nprint(1)\n```\nAfter", "Before After"),
        # Escaped markup is stripped like markup, never revived by the unescape.
        ("Text &lt;b&gt;bold&lt;/b&gt; &amp; more", "Text bold & more"),
        # Markup is recognised within a line only.
        ("Total\n-\nNext", "Total - Next"),
    ],
)
def test_markup_flattens_to_prose(answer, excerpt):
    assert plain_excerpt(answer) == excerpt


def test_a_long_answer_is_cut_at_a_word():
    excerpt = plain_excerpt("word " * 400)
    assert excerpt.endswith("…")
    assert len(excerpt) <= 321
    assert not excerpt[:-1].endswith(" ")
