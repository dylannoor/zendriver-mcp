"""Read cookies out of the user's own default browser (macOS and Linux).

Used by ``use_default_browser_cookies`` so an agent can continue in a
session the user is already logged in to. Nothing in here asks for
consent: the tool does that before it calls :func:`read_cookies`.
:func:`default_browser` and :func:`locate_cookie_store` only look at
which browser and which file, never at cookie contents, so they are safe
to run before the user has answered.

Three storage formats cover every browser we know:

- Firefox family (Firefox, Zen, LibreWolf): plain SQLite.
- Chromium family (Chrome, Arc, Brave, Edge, Vivaldi, Opera): SQLite with
  AES-encrypted values. The key lives in the macOS Keychain (macOS shows
  its own prompt the first time) or the Linux secret service.
- Safari: Apple's ``binarycookies`` file, readable only when the process
  has Full Disk Access.

Windows is not supported: Chromium there binds the cookie key to the
browser's own process.
"""

from __future__ import annotations

import configparser
import hashlib
import json
import plistlib
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from src.errors import ZendriverMCPError

# Seconds between 1601-01-01 (Chromium's epoch) / 2001-01-01 (Apple's) and 1970-01-01.
_CHROMIUM_EPOCH_OFFSET = 11_644_473_600
_APPLE_EPOCH_OFFSET = 978_307_200

# Chromium prefixes each decrypted value with SHA-256(host) from this
# cookie-database schema version on.
_CHROMIUM_HOST_HASH_VERSION = 24


@dataclass(frozen=True)
class Browser:
    """Where one browser keeps its cookies and how the OS names it."""

    name: str
    family: str  # "firefox" | "chromium" | "safari"
    mac_ids: tuple[str, ...] = ()  # bundle id prefixes, lowercase
    linux_ids: tuple[str, ...] = ()  # substrings of the xdg desktop-file id
    mac_dir: str = ""  # under ~/Library/Application Support
    linux_dirs: tuple[str, ...] = ()  # under ~
    keychain: str = ""  # macOS Keychain service is "<keychain> Safe Storage"
    linux_secret: str = ""  # ``application`` attribute in the Linux secret service


# Order matters where ids overlap: more specific entries come first.
BROWSERS: tuple[Browser, ...] = (
    Browser(
        "Zen",
        "firefox",
        mac_ids=("app.zen-browser.zen",),
        linux_ids=("zen",),
        mac_dir="zen",
        linux_dirs=(".zen", ".var/app/app.zen_browser.zen/.zen"),
    ),
    Browser(
        "LibreWolf",
        "firefox",
        mac_ids=("io.gitlab.librewolf-community", "org.mozilla.librewolf"),
        linux_ids=("librewolf",),
        mac_dir="librewolf",
        linux_dirs=(".librewolf", ".var/app/io.gitlab.librewolf-community/.librewolf"),
    ),
    Browser(
        "Firefox",
        "firefox",
        mac_ids=("org.mozilla.",),
        linux_ids=("firefox",),
        mac_dir="Firefox",
        linux_dirs=(
            ".mozilla/firefox",
            "snap/firefox/common/.mozilla/firefox",
            ".var/app/org.mozilla.firefox/.mozilla/firefox",
        ),
    ),
    Browser(
        "Arc",
        "chromium",
        mac_ids=("company.thebrowser.browser",),
        mac_dir="Arc/User Data",
        keychain="Arc",
    ),
    Browser(
        "Brave",
        "chromium",
        mac_ids=("com.brave.browser",),
        linux_ids=("brave",),
        mac_dir="BraveSoftware/Brave-Browser",
        linux_dirs=(".config/BraveSoftware/Brave-Browser",),
        keychain="Brave",
        linux_secret="brave",
    ),
    Browser(
        "Microsoft Edge",
        "chromium",
        mac_ids=("com.microsoft.edgemac",),
        linux_ids=("microsoft-edge",),
        mac_dir="Microsoft Edge",
        linux_dirs=(".config/microsoft-edge",),
        keychain="Microsoft Edge",
        linux_secret="chromium",
    ),
    Browser(
        "Vivaldi",
        "chromium",
        mac_ids=("com.vivaldi.vivaldi",),
        linux_ids=("vivaldi",),
        mac_dir="Vivaldi",
        linux_dirs=(".config/vivaldi",),
        keychain="Vivaldi",
        linux_secret="chrome",
    ),
    Browser(
        "Opera",
        "chromium",
        mac_ids=("com.operasoftware.opera",),
        linux_ids=("opera",),
        mac_dir="com.operasoftware.Opera",
        linux_dirs=(".config/opera",),
        keychain="Opera",
        linux_secret="chromium",
    ),
    Browser(
        "Chromium",
        "chromium",
        mac_ids=("org.chromium.chromium",),
        linux_ids=("chromium",),
        mac_dir="Chromium",
        linux_dirs=(".config/chromium", "snap/chromium/common/chromium"),
        keychain="Chromium",
        linux_secret="chromium",
    ),
    Browser(
        "Chrome",
        "chromium",
        mac_ids=("com.google.chrome",),
        linux_ids=("google-chrome", "chrome"),
        mac_dir="Google/Chrome",
        linux_dirs=(".config/google-chrome",),
        keychain="Chrome",
        linux_secret="chrome",
    ),
    Browser("Safari", "safari", mac_ids=("com.apple.safari",)),
)


@dataclass(frozen=True)
class BrowserCookie:
    """One cookie, in the shape CDP's ``CookieParam`` wants."""

    name: str
    value: str
    domain: str
    path: str
    secure: bool
    http_only: bool
    same_site: str | None  # "Strict" | "Lax" | "None" | None (unspecified)
    expires: float | None  # unix seconds; None for a session cookie


# ---------------------------------------------------------------- default browser


def default_browser(platform: str | None = None, home: Path | None = None) -> Browser:
    """Return the browser the OS opens https links with."""
    platform = platform or sys.platform
    home = home or Path.home()
    if platform == "darwin":
        handler = _mac_default_handler(home)
        for browser in BROWSERS:
            if any(handler.startswith(prefix) for prefix in browser.mac_ids):
                return browser
    elif platform.startswith("linux"):
        handler = _linux_default_handler()
        for browser in BROWSERS:
            if any(part in handler for part in browser.linux_ids):
                return browser
    else:
        raise ZendriverMCPError(
            f"Reading browser cookies is not supported on {platform} (macOS and Linux only)."
        )
    raise ZendriverMCPError(
        f"The default browser ({handler or 'unknown'}) is not one this server can read "
        f"cookies from. Supported: {', '.join(b.name for b in BROWSERS)}."
    )


def _mac_default_handler(home: Path) -> str:
    """Bundle id of the app registered for the https scheme, lowercase."""
    plist = (
        home / "Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist"
    )
    try:
        handlers = plistlib.loads(plist.read_bytes()).get("LSHandlers", [])
    except (OSError, plistlib.InvalidFileException):
        handlers = []
    for entry in handlers:
        if entry.get("LSHandlerURLScheme") == "https" and entry.get("LSHandlerRoleAll"):
            return str(entry["LSHandlerRoleAll"]).lower()
    # macOS only writes an entry once the user picks something else.
    return "com.apple.safari"


def _linux_default_handler() -> str:
    """Desktop-file id of the default browser (``firefox.desktop``), lowercase."""
    try:
        out = subprocess.run(
            ["xdg-settings", "get", "default-web-browser"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.strip().lower()


# ---------------------------------------------------------------- locating the store


def locate_cookie_store(
    browser: Browser, platform: str | None = None, home: Path | None = None
) -> Path:
    """Find the cookie file of the browser's active profile, without opening it."""
    platform = platform or sys.platform
    home = home or Path.home()
    if browser.family == "safari":
        candidates = [
            home / "Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies",
            home / "Library/Cookies/Cookies.binarycookies",
        ]
    else:
        roots = (
            [home / "Library/Application Support" / browser.mac_dir]
            if platform == "darwin"
            else [home / d for d in browser.linux_dirs]
        )
        find = _firefox_candidates if browser.family == "firefox" else _chromium_candidates
        candidates = [path for root in roots for path in find(root)]

    try:
        for path in candidates:
            if path.is_file():
                return path
    except PermissionError as exc:
        raise _no_access(browser, exc) from exc
    if browser.family == "safari":
        # Without Full Disk Access the container looks empty rather than forbidden.
        raise _no_access(browser, None)
    raise ZendriverMCPError(f"No cookie store found for {browser.name}. Has it ever been used?")


def _no_access(browser: Browser, cause: Exception | None) -> ZendriverMCPError:
    detail = f" ({cause})" if cause else ""
    hint = (
        " Give Full Disk Access to the app that runs this MCP server "
        "(System Settings > Privacy & Security)."
        if browser.family == "safari"
        else ""
    )
    return ZendriverMCPError(f"Cannot read {browser.name}'s cookie store{detail}.{hint}")


def _firefox_candidates(root: Path) -> list[Path]:
    """Cookie files of a Firefox-family install, the active profile first."""
    parser = configparser.RawConfigParser()
    try:
        parser.read(root / "profiles.ini", encoding="utf-8")
    except (OSError, configparser.Error):
        return []

    def profile_dir(raw: str, relative: bool = True) -> Path:
        return root / raw if relative else Path(raw)

    ordered: list[Path] = []
    # ``[Install<hash>] Default=`` is the profile the app really starts;
    # ``[Profile] Default=1`` is a legacy flag that can sit on an empty one.
    for section in parser.sections():
        if section.startswith("Install") and parser.has_option(section, "Default"):
            raw = parser.get(section, "Default")
            ordered.append(profile_dir(raw, relative=not Path(raw).is_absolute()))
    profiles = [s for s in parser.sections() if s.startswith("Profile")]
    profiles.sort(key=lambda s: parser.get(s, "Default", fallback="0") != "1")
    for section in profiles:
        if parser.has_option(section, "Path"):
            relative = parser.get(section, "IsRelative", fallback="1") == "1"
            ordered.append(profile_dir(parser.get(section, "Path"), relative))
    return [p / "cookies.sqlite" for p in ordered]


def _chromium_candidates(root: Path) -> list[Path]:
    """Cookie files of a Chromium-family install, the last used profile first."""
    profile = "Default"
    try:
        state = json.loads((root / "Local State").read_text(encoding="utf-8"))
        profile = state.get("profile", {}).get("last_used") or profile
    except (OSError, ValueError, AttributeError):
        pass
    # Opera keeps its only profile in the root itself.
    return [
        root / profile / "Network/Cookies",
        root / profile / "Cookies",
        root / "Default/Network/Cookies",
        root / "Default/Cookies",
        root / "Network/Cookies",
        root / "Cookies",
    ]


# ---------------------------------------------------------------- reading


def normalize_domains(domains: list[str] | None) -> list[str]:
    """Reduce what the caller passed (hosts, ``.host``, full URLs) to bare hostnames."""
    out: list[str] = []
    for raw in domains or []:
        value = raw.strip().lower()
        if "//" in value:
            value = urlsplit(value).hostname or ""
        value = value.split("/")[0].strip(".")
        if value and value not in out:
            out.append(value)
    return out


def _wanted(host: str, domains: list[str]) -> bool:
    """True when ``host`` is one of ``domains`` or a subdomain of one."""
    if not domains:
        return True
    host = host.lstrip(".").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def read_cookies(
    browser: Browser,
    store: Path,
    domains: list[str] | None = None,
    platform: str | None = None,
) -> list[BrowserCookie]:
    """Read the still-valid cookies for ``domains`` (all when empty) from ``store``.

    Blocking: may wait on the macOS Keychain prompt. Call it off the event loop.
    """
    wanted = normalize_domains(domains)
    try:
        if browser.family == "firefox":
            cookies = _read_firefox(store)
        elif browser.family == "chromium":
            cookies = _read_chromium(browser, store, wanted, platform or sys.platform)
        else:
            cookies = _parse_binarycookies(store.read_bytes())
    except PermissionError as exc:
        raise _no_access(browser, exc) from exc
    except sqlite3.Error as exc:
        raise ZendriverMCPError(f"Could not read {browser.name}'s cookie store: {exc}") from exc

    now = time.time()
    return [
        c for c in cookies if _wanted(c.domain, wanted) and (c.expires is None or c.expires > now)
    ]


@contextmanager
def _snapshot(db: Path) -> Iterator[sqlite3.Connection]:
    """Open a private copy of a browser's SQLite file.

    The running browser holds a lock on the original, and its newest rows
    sit in the ``-wal`` file, so both are copied to a 0700 temp dir that is
    removed again afterwards.
    """
    with tempfile.TemporaryDirectory(prefix="zendriver-cookies-") as tmp:
        copy = Path(tmp) / "cookies.db"
        shutil.copyfile(db, copy)
        for suffix in ("-wal", "-shm"):
            side = db.with_name(db.name + suffix)
            if side.exists():
                shutil.copyfile(side, copy.with_name(copy.name + suffix))
        conn = sqlite3.connect(copy)
        try:
            yield conn
        finally:
            conn.close()


def _same_site(code: int, secure: bool, strict: int, lax: int, none: int) -> str | None:
    if code == strict:
        return "Strict"
    if code == lax:
        return "Lax"
    # Chrome rejects SameSite=None without Secure, and older Firefox wrote
    # this code for cookies that never set the attribute at all.
    if code == none and secure:
        return "None"
    return None


def _read_firefox(store: Path) -> list[BrowserCookie]:
    # ponytail: persistent cookies only. Firefox keeps session cookies (no
    # expiry) in sessionstore-backups/recovery.jsonlz4, a mozlz4 file the
    # stdlib cannot unpack. Add an lz4 reader if logins that live in
    # session cookies turn out to matter.
    with _snapshot(store) as conn:
        # Only the default context: container tabs and partitioned
        # third-party cookies share this table under other originAttributes
        # and would overwrite the main session's cookie of the same name.
        rows = conn.execute(
            "SELECT name, value, host, path, expiry, isSecure, isHttpOnly, sameSite "
            "FROM moz_cookies WHERE originAttributes = ''"
        ).fetchall()
    cookies = []
    for name, value, host, path, expiry, secure, http_only, same_site in rows:
        expiry = float(expiry or 0)
        # Recent Firefox versions store milliseconds; anything this large
        # as seconds would be past the year 5000.
        if expiry > 1e11:
            expiry /= 1000
        cookies.append(
            BrowserCookie(
                name=name,
                value=value,
                domain=host,
                path=path or "/",
                secure=bool(secure),
                http_only=bool(http_only),
                same_site=_same_site(int(same_site or 0), bool(secure), strict=2, lax=1, none=0),
                expires=expiry or None,
            )
        )
    return cookies


def _read_chromium(
    browser: Browser, store: Path, domains: list[str], platform: str
) -> list[BrowserCookie]:
    with _snapshot(store) as conn:
        version_row = conn.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
        rows = conn.execute(
            "SELECT host_key, name, value, encrypted_value, path, expires_utc, "
            "is_secure, is_httponly, samesite, has_expires FROM cookies"
        ).fetchall()
    strip_hash = int(version_row[0]) >= _CHROMIUM_HOST_HASH_VERSION if version_row else False

    keys: dict[bytes, bytes] = {}  # per encryption prefix, fetched on first use
    cookies: list[BrowserCookie] = []
    failed = 0
    for row in rows:
        host, name, value, encrypted, path, expires_utc, secure, http_only, same_site, has_exp = row
        # Filter before decrypting so a per-site copy never touches other values.
        if not _wanted(host, domains):
            continue
        if not value and encrypted:
            prefix = bytes(encrypted[:3])
            try:
                if prefix not in keys:
                    keys[prefix] = _chromium_key(browser, prefix, platform)
                value = _decrypt_chromium(
                    bytes(encrypted), keys[prefix], host if strip_hash else None
                )
            except ValueError:
                failed += 1
                continue
        cookies.append(
            BrowserCookie(
                name=name,
                value=value,
                domain=host,
                path=path or "/",
                secure=bool(secure),
                http_only=bool(http_only),
                same_site=_same_site(int(same_site), bool(secure), strict=2, lax=1, none=0),
                expires=(expires_utc / 1_000_000 - _CHROMIUM_EPOCH_OFFSET)
                if has_exp and expires_utc
                else None,
            )
        )
    if failed and not cookies:
        raise ZendriverMCPError(
            f"Could not decrypt any of {browser.name}'s cookies ({failed} tried). "
            "The key from the keychain did not match."
        )
    return cookies


def _chromium_key(browser: Browser, prefix: bytes, platform: str) -> bytes:
    """Derive the AES key Chromium uses for values with this version prefix."""
    if platform == "darwin":
        password, iterations = _mac_keychain_password(f"{browser.keychain} Safe Storage"), 1003
    elif prefix == b"v10":
        # Linux without a keyring: Chromium falls back to a fixed password.
        password, iterations = b"peanuts", 1
    else:
        password, iterations = _linux_secret(browser.linux_secret), 1
    return hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", iterations, dklen=16)


def _mac_keychain_password(service: str) -> bytes:
    """Ask the macOS Keychain for a browser's storage password (may prompt the user)."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", service],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise ZendriverMCPError(f"Could not run the macOS `security` tool: {exc}") from exc
    if out.returncode != 0:
        raise ZendriverMCPError(
            f"The macOS Keychain did not release '{service}'. "
            "The Keychain prompt was denied, or that browser has never stored a key."
        )
    return out.stdout.rstrip(b"\n")


def _linux_secret(application: str) -> bytes:
    """Look up a browser's storage password in the Linux secret service."""
    try:
        out = subprocess.run(
            ["secret-tool", "lookup", "application", application],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ZendriverMCPError(
            f"Could not query the secret service for the '{application}' cookie key "
            f"(is `secret-tool` installed?): {exc}"
        ) from exc
    if out.returncode != 0 or not out.stdout:
        raise ZendriverMCPError(f"No '{application}' cookie key found in the secret service.")
    return out.stdout.rstrip(b"\n")


def _decrypt_chromium(blob: bytes, key: bytes, hashed_host: str | None) -> str:
    """AES-128-CBC decrypt one ``v10``/``v11`` cookie value.

    ``hashed_host`` is the cookie's host when the store prefixes values
    with SHA-256(host), else ``None``. Raises ``ValueError`` when the key
    was wrong: bad padding, a host hash that doesn't match, or bytes that
    are not UTF-8.
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    payload = blob[3:]
    if not payload or len(payload) % 16:
        raise ValueError("ciphertext is not a whole number of AES blocks")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
    plain = decryptor.update(payload) + decryptor.finalize()
    pad = plain[-1]
    if not 1 <= pad <= 16 or plain[-pad:] != bytes([pad]) * pad:
        raise ValueError("bad padding")
    plain = plain[:-pad]
    if hashed_host is not None:
        # About 1 in 256 wrong-key decrypts ends in valid padding; the hash
        # is what tells a real value from that garbage.
        if plain[:32] != hashlib.sha256(hashed_host.encode()).digest():
            raise ValueError("host hash mismatch")
        plain = plain[32:]
    return plain.decode("utf-8")


def _parse_binarycookies(data: bytes) -> list[BrowserCookie]:
    """Parse Safari's ``Cookies.binarycookies``.

    Layout: ``cook`` magic, big-endian page count and page sizes, then
    pages. Each page is little-endian: a 4-byte tag, the cookie count, an
    offset per cookie, and the cookie records those offsets point at.
    """
    if data[:4] != b"cook":
        raise ZendriverMCPError("Safari's cookie file has an unknown format.")
    try:
        (page_count,) = struct.unpack_from(">I", data, 4)
        sizes = struct.unpack_from(f">{page_count}I", data, 8)
        cookies: list[BrowserCookie] = []
        start = 8 + 4 * page_count
        for size in sizes:
            page = data[start : start + size]
            start += size
            (count,) = struct.unpack_from("<I", page, 4)
            for offset in struct.unpack_from(f"<{count}I", page, 8):
                cookies.append(_parse_binarycookie(page, offset))
        return cookies
    except (struct.error, IndexError, ValueError) as exc:
        raise ZendriverMCPError(f"Safari's cookie file is damaged or truncated: {exc}") from exc


def _parse_binarycookie(page: bytes, offset: int) -> BrowserCookie:
    _size, _, flags, _, domain_at, name_at, path_at, value_at = struct.unpack_from(
        "<8I", page, offset
    )
    (expires,) = struct.unpack_from("<d", page, offset + 40)

    def text(at: int) -> str:
        begin = offset + at
        return page[begin : page.index(b"\x00", begin)].decode("utf-8", errors="replace")

    return BrowserCookie(
        name=text(name_at),
        value=text(value_at),
        domain=text(domain_at),
        path=text(path_at) or "/",
        secure=bool(flags & 1),
        http_only=bool(flags & 4),
        same_site=None,  # not stored in this format
        expires=expires + _APPLE_EPOCH_OFFSET if expires else None,
    )


def summarize_domains(cookies: list[BrowserCookie], limit: int = 15) -> dict[str, Any]:
    """Count cookies per site for the tool response, never exposing values."""
    counts: dict[str, int] = {}
    for cookie in cookies:
        host = cookie.domain.lstrip(".")
        counts[host] = counts.get(host, 0) + 1
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return {"sites": len(counts), "top": dict(top[:limit])}
