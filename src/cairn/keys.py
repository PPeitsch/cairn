"""The node key: ed25519, in OpenSSH format.

One key per node, for two things:

- **signing the events** it writes: the signature covers the event `id`, with a domain prefix
  (`cairn-event-v1\\0`) so that an event signature is useless in any other protocol;
- **authenticating over ssh** when syncing: the file is a plain OpenSSH private key
  (`ssh -i <data>/node_key`), and its public half is the line that goes into the other side's
  `authorized_keys`.

The public key is written as an `authorized_keys` line: `ssh-ed25519 AAAA… cairn:<node>`.
Its fingerprint (`SHA256:…`, the same one `ssh-keygen -lf` prints) is the short form to note
down outside the node: it is what `sync --restore --old-key` asks for, so a peer's old keys are
not taken on faith.
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

SIG_DOMAIN = b"cairn-event-v1\0"


class KeyFileError(ValueError):
    """A key that cannot be read or is not ed25519."""


def generate(path: Path, node: str) -> Ed25519PrivateKey:
    """Creates the node key (private with mode 0600, public next to it as `.pub`)."""
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                            serialization.NoEncryption())
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    path.with_suffix(".pub").write_text(public_line(key.public_key(), node) + "\n", encoding="utf-8")
    return key


def load_private(path: Path) -> Ed25519PrivateKey:
    try:
        key = serialization.load_ssh_private_key(path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as e:
        raise KeyFileError(f"{path}: could not read the node key ({e})") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise KeyFileError(f"{path}: the node key is not ed25519")
    return key


def public_line(pub: Ed25519PublicKey, node: str) -> str:
    raw = pub.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
    return f"{raw.decode('ascii')} cairn:{node}"


def parse_public(line: str) -> Ed25519PublicKey:
    """A public key in `ssh-ed25519 AAAA… [comment]` format."""
    parts = line.strip().split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise KeyFileError("expected a public key `ssh-ed25519 AAAA…`")
    try:
        key = serialization.load_ssh_public_key(f"{parts[0]} {parts[1]}".encode("ascii"))
    except (ValueError, UnicodeEncodeError) as e:
        raise KeyFileError(f"invalid public key: {e}") from None
    if not isinstance(key, Ed25519PublicKey):
        raise KeyFileError("the public key is not ed25519")
    return key


def normalize_public(line: str) -> str:
    """`ssh-ed25519 AAAA…` without the comment: the form that is stored and compared."""
    parse_public(line)
    return " ".join(line.split()[:2])


def fingerprint(line: str) -> str:
    """`SHA256:…` of a public key, same as `ssh-keygen -lf`."""
    parse_public(line)
    blob = base64.b64decode(line.split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")


def as_fingerprint(value: str) -> str:
    """A `SHA256:…` fingerprint, or a whole public key turned into one (for comparing)."""
    value = value.strip()
    if value.startswith("SHA256:"):
        body = value[len("SHA256:"):]
        try:
            ok = len(base64.b64decode(body + "=" * (-len(body) % 4), validate=True)) == 32
        except ValueError:
            ok = False
        if not ok:
            raise KeyFileError(f"invalid fingerprint: {value} (expected `SHA256:` and 43 characters)")
        return value
    return fingerprint(value)


def sign(key: Ed25519PrivateKey, event_id: str) -> str:
    return base64.b64encode(key.sign(SIG_DOMAIN + event_id.encode("ascii"))).decode("ascii")


def verify(pub: Ed25519PublicKey, event_id: str, sig: str | None) -> bool:
    if not sig:
        return False
    try:
        pub.verify(base64.b64decode(sig, validate=True), SIG_DOMAIN + event_id.encode("ascii"))
    except (InvalidSignature, ValueError):
        return False
    return True
