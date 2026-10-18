"""Load an Outlook (Windows) HTML signature and its embedded images."""

from __future__ import annotations

import re
import urllib.parse
from pathlib import Path


def list_signatures(sig_dir: Path | None) -> list[str]:
    if not sig_dir or not sig_dir.is_dir():
        return []
    names = set()
    for p in sig_dir.glob("*.htm"):
        names.add(re.sub(r" \([^)]*\)$", "", p.stem))
    return sorted(names)


def load_signature(name: str, sig_dir: Path | None) -> tuple[str, dict[str, str]]:
    """Return ``(inner_html, {content_id: image_path})`` for the named signature.

    Outlook stores signatures as ``<name>.htm`` or ``<name> (<account>).htm`` with
    images in a ``<name>_files`` folder next to it.
    """
    if not sig_dir or not sig_dir.is_dir():
        raise FileNotFoundError("No signature directory configured (EXCHANGE_SIGNATURE_DIR).")
    if any(sep in name for sep in ("/", "\\", "..")):
        raise ValueError("Signature name must not contain path separators.")
    candidates = sorted(sig_dir.glob(f"{glob_escape(name)} (*).htm")) or sorted(sig_dir.glob(f"{glob_escape(name)}.htm"))
    if not candidates:
        available = ", ".join(list_signatures(sig_dir)) or "none"
        raise FileNotFoundError(f"Signature {name!r} not found in {sig_dir} (available: {available}).")
    path = candidates[0]
    raw = path.read_bytes()
    match = re.search(rb'charset=["\']?([\w-]+)', raw[:3000], re.I)
    encoding = match.group(1).decode() if match else "utf-8"
    try:
        html = raw.decode(encoding)
    except (LookupError, UnicodeDecodeError):
        html = raw.decode("utf-8", errors="replace")
    body = re.search(r"<body[^>]*>(.*)</body>", html, re.S | re.I)
    inner = body.group(1) if body else html
    inner = re.sub(r"<!--\[if gte vml 1\]>.*?<!\[endif\]-->", "", inner, flags=re.S)
    inner = re.sub(r"<!\[if !vml\]>|<!\[endif\]>", "", inner)

    images: dict[str, str] = {}
    base = path.parent.resolve()

    def _replace(m: re.Match) -> str:
        src = m.group(1)
        if src.startswith(("cid:", "http:", "https:", "data:")):
            return m.group(0)
        img = (path.parent / urllib.parse.unquote(src)).resolve()
        if not img.is_file() or base not in img.parents:
            return m.group(0)
        cid = "sig_" + re.sub(r"[^A-Za-z0-9_.-]", "_", img.name)
        images[cid] = str(img)
        return f'src="cid:{cid}"'

    inner = re.sub(r'src="([^"]+)"', _replace, inner)
    return inner, images


def glob_escape(text: str) -> str:
    return re.sub(r"([\[\]*?])", r"[\1]", text)
