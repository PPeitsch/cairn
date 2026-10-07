"""La clave del nodo: ed25519, en formato OpenSSH.

Una sola clave por nodo, para dos cosas:

- **firmar los eventos** que escribe: la firma va sobre el `id` del evento, con un prefijo de
  dominio (`cairn-event-v1\\0`) para que una firma de evento no sirva en otro protocolo;
- **autenticarse por ssh** al sincronizar: el archivo es una clave privada OpenSSH común
  (`ssh -i <datos>/node_key`), y su pública es la línea que va al `authorized_keys` del otro.

La pública se escribe como una línea de `authorized_keys`: `ssh-ed25519 AAAA… cairn:<nodo>`.
Su huella (`SHA256:…`, la misma que da `ssh-keygen -lf`) es la forma corta de anotarla fuera del
nodo: es lo que pide `sync --restore --old-key` para no tomar a ciegas las claves viejas de un par.
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
    """Una clave que no se puede leer o no es ed25519."""


def generate(path: Path, node: str) -> Ed25519PrivateKey:
    """Crea la clave del nodo (privada con permisos 0600, pública al lado en `.pub`)."""
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
        raise KeyFileError(f"{path}: no se pudo leer la clave del nodo ({e})") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise KeyFileError(f"{path}: la clave del nodo no es ed25519")
    return key


def public_line(pub: Ed25519PublicKey, node: str) -> str:
    raw = pub.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
    return f"{raw.decode('ascii')} cairn:{node}"


def parse_public(line: str) -> Ed25519PublicKey:
    """Una pública en formato `ssh-ed25519 AAAA… [comentario]`."""
    parts = line.strip().split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise KeyFileError("se espera una clave pública `ssh-ed25519 AAAA…`")
    try:
        key = serialization.load_ssh_public_key(f"{parts[0]} {parts[1]}".encode("ascii"))
    except (ValueError, UnicodeEncodeError) as e:
        raise KeyFileError(f"clave pública inválida: {e}") from None
    if not isinstance(key, Ed25519PublicKey):
        raise KeyFileError("la clave pública no es ed25519")
    return key


def normalize_public(line: str) -> str:
    """`ssh-ed25519 AAAA…` sin el comentario: la forma que se guarda y se compara."""
    parse_public(line)
    return " ".join(line.split()[:2])


def fingerprint(line: str) -> str:
    """`SHA256:…` de una pública, igual que `ssh-keygen -lf`."""
    parse_public(line)
    blob = base64.b64decode(line.split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")


def as_fingerprint(value: str) -> str:
    """Una huella `SHA256:…` o una pública entera, llevada a huella (para comparar)."""
    value = value.strip()
    if value.startswith("SHA256:"):
        body = value[len("SHA256:"):]
        try:
            ok = len(base64.b64decode(body + "=" * (-len(body) % 4), validate=True)) == 32
        except ValueError:
            ok = False
        if not ok:
            raise KeyFileError(f"huella inválida: {value} (se espera `SHA256:` y 43 caracteres)")
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
