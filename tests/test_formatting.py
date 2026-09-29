from codex_telegram_bot.formatting import (
    format_bytes,
    format_duration,
    format_tokens,
    render_chunks,
    render_inline,
    split_segments,
    strip_html,
    truncate,
)


def test_code_block_rendered_as_pre():
    chunks = render_chunks("Текст\n```python\nprint('<hi>')\n```\nконец")
    assert len(chunks) == 1
    assert '<pre><code class="language-python">print(&#x27;&lt;hi&gt;&#x27;)</code></pre>' in chunks[0]
    assert chunks[0].startswith("Текст\n")
    assert chunks[0].endswith("\nконец")


def test_inline_marks():
    out = render_inline("**жирный** и `код <b>` и *курсив* и [ссылка](https://example.com/?a=1&b=2) ~~зач~~")
    assert "<b>жирный</b>" in out
    assert "<code>код &lt;b&gt;</code>" in out
    assert "<i>курсив</i>" in out
    assert '<a href="https://example.com/?a=1&amp;b=2">ссылка</a>' in out
    assert "<s>зач</s>" in out


def test_snake_case_and_math_untouched():
    text = "переменная my_var_name и a*b*c, 2 < 3 && x"
    assert render_inline(text) == "переменная my_var_name и a*b*c, 2 &lt; 3 &amp;&amp; x"


def test_header_bullets_and_rule():
    chunks = render_chunks("# Заголовок\n- пункт\n* второй\n---")
    assert chunks == ["<b>Заголовок</b>\n• пункт\n• второй\n———"]


def test_split_segments_unterminated():
    segments = split_segments("```\nabc")
    assert [(s.kind, s.body) for s in segments] == [("code", "abc")]
    assert render_chunks("```\nabc") == ["<pre><code>abc</code></pre>"]


def test_long_text_split_respects_limit():
    md = "\n".join(f"строка {i} " + "x" * 50 for i in range(400))
    chunks = render_chunks(md, limit=1000)
    assert len(chunks) > 1
    assert all(len(c) <= 1000 for c in chunks)
    assert "".join(strip_html(c) for c in chunks).replace("\n", "") == md.replace("\n", "")


def test_long_code_split_keeps_pre_balanced():
    md = "```\n" + "\n".join(f"line {i} <tag> & stuff" for i in range(500)) + "\n```"
    chunks = render_chunks(md, limit=1500)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 1500
        assert chunk.count("<pre>") == chunk.count("</pre>") >= 1
        assert chunk.startswith("<pre>") and chunk.endswith("</pre>")
    assert "".join(strip_html(c) for c in chunks).replace("\n", "") == md[4:-4].replace("\n", "")


def test_very_long_single_line():
    md = "&" * 5000
    chunks = render_chunks(md, limit=1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert "".join(strip_html(c) for c in chunks) == md


def test_table_in_pre():
    md = "| a | b |\n|---|---|\n| 1 | 2 |"
    assert render_chunks(md) == ["<pre>| a | b |\n|---|---|\n| 1 | 2 |</pre>"]


def test_empty_input():
    assert render_chunks("") == [""]


def test_helpers():
    assert format_duration(59) == "59с"
    assert format_duration(61) == "1м 01с"
    assert format_duration(3601) == "1ч 00м 01с"
    assert format_duration(90000) == "1д 1ч 00м"
    assert truncate("a" * 10, 5) == "aaaa…"
    assert truncate("ab\ncd", 10) == "ab cd"


def test_format_tokens():
    assert format_tokens(12) == "12"
    assert format_tokens(999) == "999"
    assert format_tokens(12345) == "12.3k"
    assert format_tokens(1_234_567) == "1.23M"


def test_format_bytes():
    assert format_bytes(0) == "0 Б"
    assert format_bytes(1536) == "1.5 КБ"
    assert format_bytes(3_000_000) == "2.9 МБ"
    assert format_bytes(5 * 1024**3) == "5.0 ГБ"
