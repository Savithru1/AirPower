"""Prepare a deployment: random secrets, and optionally a self-signed certificate for a demo.

    python -m tools.init_deploy [--demo-cert] [--force]

Writes deploy/secrets/ (airpower_secret.txt: signs session cookies; airpower_admin_password.txt: the first
administrator's temporary password, which must be changed at first sign-in; airpower_db_password.txt:
PostgreSQL). Existing files are kept unless --force. The values are not printed. Both directories are
git-ignored. With --demo-cert, deploy/certs/server.{crt,key} is a self-signed certificate for localhost:
a real deployment uses a certificate from the unit's own PKI instead.
"""
import argparse
import datetime
import ipaddress
import os
import secrets
import stat
import sys

from airpower.config import ROOT

DEPLOY = ROOT / "deploy"


def _write(path, value, force):
    if path.exists() and not force:
        print(f"kept    {path.relative_to(ROOT)}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    print(f"wrote   {path.relative_to(ROOT)}")


def demo_cert(force):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    crt, key = DEPLOY / "certs" / "server.crt", DEPLOY / "certs" / "server.key"
    if crt.exists() and key.exists() and not force:
        print(f"kept    {crt.relative_to(ROOT)}")
        return
    k = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "AirPower demo (self-signed)")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=90))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
            .sign(k, hashes.SHA256()))
    crt.parent.mkdir(parents=True, exist_ok=True)
    crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                    serialization.NoEncryption()))
    try:
        os.chmod(key, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    print(f"wrote   {crt.relative_to(ROOT)} and server.key (self-signed, valid 90 days, localhost only)")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--demo-cert", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    s = DEPLOY / "secrets"
    _write(s / "airpower_secret.txt", secrets.token_hex(32), a.force)
    _write(s / "airpower_admin_password.txt", secrets.token_urlsafe(18), a.force)
    _write(s / "airpower_db_password.txt", secrets.token_urlsafe(24), a.force)
    if a.demo_cert:
        demo_cert(a.force)
    print("The administrator is 'admin'; read the temporary password from deploy/secrets/airpower_admin_password.txt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
