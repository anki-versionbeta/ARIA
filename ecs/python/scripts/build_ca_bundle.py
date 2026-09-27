"""Build a CA bundle trusting both the AbbVie TLS-interception chain and public roots.

The corporate proxy re-signs traffic to intercepted hosts, so the AbbVie chain is
required. But Artifactory redirects package downloads to S3, which is *not*
intercepted and needs the ordinary public roots. Trusting only one of the two
fails half the requests, which is why disabling verification is the usual (wrong)
workaround. Concatenating both lets verification stay on everywhere.

Run:  python scripts/build_ca_bundle.py
Out:  certs/ca-bundle.pem
"""

from __future__ import annotations

import pathlib
import ssl
import sys

ABBVIE_CHAIN = pathlib.Path(r"C:\AbbVieFullChain_pem\AbbVieFullChain.pem")
OUT = pathlib.Path(__file__).resolve().parent.parent / "certs" / "ca-bundle.pem"


def certifi_roots() -> str:
    """Public roots. Needed because Artifactory redirects downloads to S3, which
    the proxy does NOT intercept and which therefore presents a normal Amazon chain."""
    try:
        import certifi
    except ImportError:
        sys.exit("certifi not available; cannot assemble public roots")
    return pathlib.Path(certifi.where()).read_text(encoding="utf-8")


def main() -> None:
    if not ABBVIE_CHAIN.is_file():
        sys.exit(f"AbbVie chain not found at {ABBVIE_CHAIN}")

    # Do NOT add the whole Windows trust store here. It contains a certificate
    # with a non-positive serial number, which makes OpenSSL abandon the rest of
    # the file and produces confusing "unable to get local issuer" errors.
    parts = [
        "# --- public roots (certifi): S3, Amazon, general internet ---",
        certifi_roots(),
        "# --- AbbVie chain: proxy-intercepted hosts and abbvienet.com ---",
        ABBVIE_CHAIN.read_text(encoding="utf-8"),
    ]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(parts), encoding="utf-8")

    count = OUT.read_text(encoding="utf-8").count("BEGIN CERTIFICATE")
    print(f"wrote {OUT}  ({count} certificates)")


if __name__ == "__main__":
    main()
