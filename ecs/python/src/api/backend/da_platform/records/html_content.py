"""Sanitise section HTML at the point it enters the system.

Section content arrives from a rich-text editor in the browser, so it is untrusted
input at a trust boundary. Two reasons this exists:

1. Quill 2.0.3 carries a known XSS advisory in its HTML export
   (GHSA-v3m3-f69x-jf25) — the exact feature used to produce this content. The
   published fix is a downgrade flagged as breaking, so the boundary is defended
   here instead, which holds regardless of editor version.
2. The docx emitter only honours a handful of tags. Anything else is silently
   dropped at render time, so accepting it merely stores confusing content.

The allowlist is deliberately the set the emitter understands, plus `data-list`,
which editors use to mark a bullet inside an `<ol>`.
"""

from __future__ import annotations

import bleach

ALLOWED_TAGS = [
    "p",
    "br",
    "div",
    "ul",
    "ol",
    "li",
    "b",
    "strong",
    "i",
    "em",
    "u",
]

ALLOWED_ATTRIBUTES = {
    # Editors that emit every list as <ol> distinguish bullets with this attribute,
    # and the emitter reads it.
    "li": ["data-list"],
}


def sanitize_section_html(html: str) -> str:
    """Strip anything outside the allowlist. Disallowed tags are removed, not escaped,
    so a paste from Word does not leave visible markup in the document."""
    if not html:
        return ""
    return bleach.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        strip=True,
        strip_comments=True,
    )
