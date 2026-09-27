"""Where this silo's objects live in the shared bucket.

Its own module so both `silo.py` and `router.py` can use it without importing each
other. A location, not a credential — the bucket and credential chain stay with the
platform.
"""

STORAGE_PREFIX = "BOP"
