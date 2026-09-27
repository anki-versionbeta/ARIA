"""The sanitiser that guards the section-content trust boundary.

Section HTML comes from a rich-text editor in the browser, so it is untrusted input.
Quill 2.0.3 carries a known XSS advisory in the very HTML-export feature that produces
this content (GHSA-v3m3-f69x-jf25), and the published fix is a breaking downgrade, so the
boundary is defended here instead. That makes this module worth testing directly rather
than only through the endpoints that happen to call it.

Two properties matter and pull against each other: nothing dangerous may survive, and
everything the docx emitter understands must survive — a sanitiser that ate legitimate
formatting would quietly damage authored documents.
"""

from __future__ import annotations

import pytest

from api.backend.da_platform.records.html_content import (
    ALLOWED_ATTRIBUTES,
    ALLOWED_TAGS,
    sanitize_section_html,
)

# ── nothing dangerous survives ────────────────────────────────────────────────


def test_a_script_element_does_not_survive():
    result = sanitize_section_html("<p>Before</p><script>alert(1)</script><p>After</p>")

    assert "<script" not in result.lower()
    # And it is removed, not escaped into something a later renderer might revive.
    assert "&lt;script" not in result.lower()
    assert "Before" in result and "After" in result


def test_an_event_handler_attribute_is_stripped():
    result = sanitize_section_html('<p onclick="steal()">Text</p>')

    assert "onclick" not in result.lower()
    assert "steal" not in result
    assert "Text" in result


@pytest.mark.parametrize(
    "handler",
    ["onclick", "onerror", "onload", "onmouseover", "onfocus"],
)
def test_no_event_handler_survives_on_an_allowed_tag(handler):
    result = sanitize_section_html(f'<div {handler}="x()">Body</div>')

    assert handler not in result.lower()
    assert "Body" in result


def test_an_image_with_an_error_handler_is_removed_entirely():
    # <img> is not in the allowlist, so the classic onerror payload has no carrier.
    result = sanitize_section_html('<img src=x onerror="alert(1)">')

    assert "<img" not in result.lower()
    assert "onerror" not in result.lower()


@pytest.mark.parametrize("tag", ["iframe", "object", "embed", "form", "style", "svg"])
def test_dangerous_containers_do_not_survive(tag):
    result = sanitize_section_html(f"<{tag}>payload</{tag}>")

    assert f"<{tag}" not in result.lower()


def test_a_link_carrying_a_javascript_url_is_removed():
    # <a> is not allowed at all, which removes the whole javascript: question.
    result = sanitize_section_html('<a href="javascript:alert(1)">Click</a>')

    assert "<a" not in result.lower()
    assert "javascript:" not in result.lower()


def test_comments_are_dropped():
    result = sanitize_section_html("<p>Kept</p><!-- a comment -->")

    assert "a comment" not in result
    assert "Kept" in result


def test_a_style_attribute_on_an_allowed_tag_is_stripped():
    result = sanitize_section_html('<p style="position:fixed;top:0">Text</p>')

    assert "style" not in result.lower()
    assert "Text" in result


def test_an_unexpected_attribute_on_an_allowed_tag_is_stripped():
    result = sanitize_section_html('<p class="x" id="y" data-foo="z">Text</p>')

    for attribute in ("class=", "id=", "data-foo"):
        assert attribute not in result
    assert "Text" in result


# ── everything the emitter understands survives ──────────────────────────────


@pytest.mark.parametrize("tag", ALLOWED_TAGS)
def test_every_allowed_tag_survives(tag):
    """The allowlist is the set the docx emitter honours, so each entry must round-trip.

    A tag silently dropped here would be dropped from the built document too.
    """
    if tag == "br":
        assert "<br" in sanitize_section_html("<p>a<br>b</p>").lower()
        return
    result = sanitize_section_html(f"<{tag}>content</{tag}>")

    assert f"<{tag}" in result.lower()
    assert "content" in result


def test_a_nested_list_structure_survives():
    html = "<ul><li>One</li><li>Two</li></ul><ol><li>First</li></ol>"

    result = sanitize_section_html(html)

    assert result.count("<li") == 3
    assert "<ul" in result and "<ol" in result


def test_the_data_list_attribute_survives_on_a_list_item():
    # Editors that emit every list as <ol> mark bullets with this, and the emitter reads
    # it, so it is the one attribute the allowlist keeps.
    assert "li" in ALLOWED_ATTRIBUTES

    result = sanitize_section_html('<ol><li data-list="bullet">Item</li></ol>')

    assert "data-list" in result
    assert "bullet" in result


def test_the_data_list_attribute_is_not_allowed_elsewhere():
    result = sanitize_section_html('<p data-list="bullet">Item</p>')

    assert "data-list" not in result


def test_inline_emphasis_survives():
    html = "<p><b>bold</b> <strong>strong</strong> <i>i</i> <em>em</em> <u>u</u></p>"

    result = sanitize_section_html(html)

    for tag in ("b", "strong", "i", "em", "u"):
        assert f"<{tag}>" in result


# ── shape and edge cases ─────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["", None])
def test_empty_input_becomes_an_empty_string(value):
    # Callers store the result directly, so None must not reach the database.
    assert sanitize_section_html(value) == ""


def test_plain_text_is_left_alone():
    assert sanitize_section_html("Just words.") == "Just words."


def test_disallowed_markup_is_stripped_rather_than_escaped():
    """strip=True is deliberate: a paste from Word must not leave visible tags.

    Escaping would show `<font>` to the author as literal text in their document.
    """
    result = sanitize_section_html("<font face='Arial'>Pasted</font>")

    assert "<" not in result
    assert result.strip() == "Pasted"


def test_unclosed_markup_does_not_raise():
    # Editors emit fragments; this must never be the thing that fails a save.
    assert sanitize_section_html("<p>Unclosed") is not None
    assert sanitize_section_html("<ul><li>a") is not None


def test_the_result_is_always_a_string():
    for value in ("<p>x</p>", "", None, "plain"):
        assert isinstance(sanitize_section_html(value), str)


def test_sanitising_twice_changes_nothing_further():
    """Saving an already-sanitised body must be stable.

    Sections are re-saved on every edit, so a sanitiser that kept rewriting its own
    output would make the stored content drift.
    """
    once = sanitize_section_html("<p>Text <b>bold</b></p><script>x</script>")

    assert sanitize_section_html(once) == once
