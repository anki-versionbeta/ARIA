"""Determine the minimal CA set needed for each host the toolchain talks to."""

import pathlib
import ssl

import certifi
import requests
from cryptography import x509

ABBVIE = pathlib.Path(r"C:\AbbVieFullChain_pem\AbbVieFullChain.pem")
TMP = pathlib.Path(__file__).resolve().parent.parent / "certs"
TMP.mkdir(parents=True, exist_ok=True)

HOSTS = {
    "artifactory": "https://abbvie.jfrog.io/artifactory/api/npm/npm/",
    "artifactory-s3": "https://jfrog-prod-use1-shared-virginia-main.s3.amazonaws.com/",
}

# Candidate bundles, cheapest first.
certifi_only = TMP / "_t_certifi.pem"
certifi_only.write_text(pathlib.Path(certifi.where()).read_text(encoding="utf-8"), encoding="utf-8")

certifi_abbvie = TMP / "_t_certifi_abbvie.pem"
certifi_abbvie.write_text(
    pathlib.Path(certifi.where()).read_text(encoding="utf-8")
    + "\n"
    + ABBVIE.read_text(encoding="utf-8"),
    encoding="utf-8",
)

# Pull just the Zscaler roots out of the Windows store by name.
zs_pems = []
for store in ("ROOT", "CA"):
    try:
        entries = ssl.enum_certificates(store)
    except Exception:
        continue
    for der, enc, _ in entries:
        if enc != "x509_asn":
            continue
        try:
            c = x509.load_der_x509_certificate(der)
            subject = c.subject.rfc4514_string().upper()
        except Exception:
            continue
        if "ZSCALER" in subject:
            zs_pems.append((subject, ssl.DER_cert_to_PEM_cert(der)))

print(f"Zscaler certs found in Windows store: {len(zs_pems)}")
for s, _ in zs_pems:
    print("   ", s[:88])

full = TMP / "_t_full.pem"
full.write_text(
    pathlib.Path(certifi.where()).read_text(encoding="utf-8")
    + "\n"
    + ABBVIE.read_text(encoding="utf-8")
    + "\n"
    + "\n".join(p for _, p in zs_pems),
    encoding="utf-8",
)

bundles = {
    "certifi only": certifi_only,
    "certifi+abbvie": certifi_abbvie,
    "certifi+abbvie+zscaler": full,
}

print()
for hname, url in HOSTS.items():
    print(hname)
    for bname, path in bundles.items():
        try:
            r = requests.get(url, timeout=25, verify=str(path))
            print(f"    {bname:24s} -> TLS OK (HTTP {r.status_code})")
        except requests.exceptions.SSLError as e:
            msg = str(e)
            reason = "unable to get local issuer" if "local issuer" in msg else "self-signed in chain" if "self-signed" in msg else msg[:50]
            print(f"    {bname:24s} -> SSL FAIL: {reason}")
        except Exception as e:
            print(f"    {bname:24s} -> {type(e).__name__}: {str(e)[:50]}")
    print()
