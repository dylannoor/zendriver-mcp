"""Reading cookies out of Firefox-, Chromium- and Safari-style stores."""

from __future__ import annotations

import hashlib
import plistlib
import sqlite3
import struct
import time
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from src import browser_cookies as bc
from src.errors import ZendriverMCPError

FUTURE = int(time.time()) + 86_400
PAST = int(time.time()) - 86_400


def _browser(name: str) -> bc.Browser:
    return next(b for b in bc.BROWSERS if b.name == name)


# ---------------------------------------------------------------- default browser


def _write_handlers(home: Path, handlers: list[dict[str, str]]) -> None:
    plist = (
        home / "Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist"
    )
    plist.parent.mkdir(parents=True)
    plist.write_bytes(plistlib.dumps({"LSHandlers": handlers}, fmt=plistlib.FMT_BINARY))


@pytest.mark.parametrize(
    ("bundle_id", "expected"),
    [
        ("app.zen-browser.zen", "Zen"),
        ("com.google.Chrome", "Chrome"),  # macOS stores mixed case
        ("org.mozilla.firefoxdeveloperedition", "Firefox"),
        ("org.mozilla.librewolf", "LibreWolf"),
        ("company.thebrowser.Browser", "Arc"),
        ("com.apple.Safari", "Safari"),
    ],
)
def test_mac_default_browser_from_the_https_handler(
    tmp_path: Path, bundle_id: str, expected: str
) -> None:
    _write_handlers(
        tmp_path,
        [
            {"LSHandlerURLScheme": "mailto", "LSHandlerRoleAll": "com.apple.mail"},
            {"LSHandlerURLScheme": "https", "LSHandlerRoleAll": bundle_id},
        ],
    )
    assert bc.default_browser("darwin", tmp_path).name == expected


def test_mac_without_an_https_handler_means_safari(tmp_path: Path) -> None:
    # macOS writes no entry until the user picks another browser
    assert bc.default_browser("darwin", tmp_path).name == "Safari"


def test_unknown_default_browser_is_named_in_the_error(tmp_path: Path) -> None:
    _write_handlers(
        tmp_path, [{"LSHandlerURLScheme": "https", "LSHandlerRoleAll": "com.kagi.orion"}]
    )
    with pytest.raises(ZendriverMCPError, match="com.kagi.orion"):
        bc.default_browser("darwin", tmp_path)


@pytest.mark.parametrize(
    ("desktop_id", "expected"),
    [
        ("google-chrome.desktop", "Chrome"),
        ("chromium-browser.desktop", "Chromium"),
        ("brave-browser.desktop", "Brave"),
        ("firefox_firefox.desktop", "Firefox"),
        ("zen-browser.desktop", "Zen"),
    ],
)
def test_linux_default_browser_from_the_desktop_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, desktop_id: str, expected: str
) -> None:
    monkeypatch.setattr(bc, "_linux_default_handler", lambda: desktop_id)
    assert bc.default_browser("linux", tmp_path).name == expected


def test_linux_without_a_default_browser_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(bc, "_linux_default_handler", lambda: "")
    with pytest.raises(ZendriverMCPError, match="unknown"):
        bc.default_browser("linux", tmp_path)


def test_windows_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ZendriverMCPError, match="not supported on win32"):
        bc.default_browser("win32", tmp_path)


# ---------------------------------------------------------------- locating the store


def _zen_root(home: Path) -> Path:
    root = home / "Library/Application Support/zen"
    root.mkdir(parents=True)
    return root


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def test_firefox_install_default_beats_the_legacy_default_flag(tmp_path: Path) -> None:
    """Zen marks an empty profile ``Default=1`` while the install runs another one."""
    root = _zen_root(tmp_path)
    (root / "profiles.ini").write_text(
        "[Install6ED35B3CA1B5D3AF]\nDefault=Profiles/real.Default (release)\nLocked=1\n\n"
        "[Profile1]\nName=Default Profile\nIsRelative=1\nPath=Profiles/empty.Default Profile\n"
        "Default=1\n\n"
        "[Profile0]\nName=Default (release)\nIsRelative=1\nPath=Profiles/real.Default (release)\n"
    )
    real = _touch(root / "Profiles/real.Default (release)/cookies.sqlite")
    _touch(root / "Profiles/empty.Default Profile/cookies.sqlite")
    assert bc.locate_cookie_store(_browser("Zen"), "darwin", tmp_path) == real


def test_firefox_falls_back_to_the_default_flagged_profile(tmp_path: Path) -> None:
    root = _zen_root(tmp_path)
    (root / "profiles.ini").write_text(
        "[Profile0]\nName=a\nIsRelative=1\nPath=Profiles/a\n\n"
        "[Profile1]\nName=b\nIsRelative=1\nPath=Profiles/b\nDefault=1\n"
    )
    _touch(root / "Profiles/a/cookies.sqlite")
    flagged = _touch(root / "Profiles/b/cookies.sqlite")
    assert bc.locate_cookie_store(_browser("Zen"), "darwin", tmp_path) == flagged


def test_firefox_absolute_profile_path(tmp_path: Path) -> None:
    root = _zen_root(tmp_path)
    elsewhere = _touch(tmp_path / "elsewhere/profile/cookies.sqlite")
    (root / "profiles.ini").write_text(
        f"[Profile0]\nName=a\nIsRelative=0\nPath={elsewhere.parent}\n"
    )
    assert bc.locate_cookie_store(_browser("Zen"), "darwin", tmp_path) == elsewhere


def test_firefox_linux_checks_every_known_location(tmp_path: Path) -> None:
    root = tmp_path / "snap/firefox/common/.mozilla/firefox"
    root.mkdir(parents=True)
    (root / "profiles.ini").write_text("[Profile0]\nName=a\nIsRelative=1\nPath=abc.default\n")
    store = _touch(root / "abc.default/cookies.sqlite")
    assert bc.locate_cookie_store(_browser("Firefox"), "linux", tmp_path) == store


def test_chromium_uses_the_last_used_profile(tmp_path: Path) -> None:
    root = tmp_path / "Library/Application Support/Google/Chrome"
    root.mkdir(parents=True)
    (root / "Local State").write_text('{"profile": {"last_used": "Profile 2"}}')
    _touch(root / "Default/Network/Cookies")
    last_used = _touch(root / "Profile 2/Network/Cookies")
    assert bc.locate_cookie_store(_browser("Chrome"), "darwin", tmp_path) == last_used


def test_chromium_without_local_state_uses_default(tmp_path: Path) -> None:
    root = tmp_path / "Library/Application Support/Google/Chrome"
    store = _touch(root / "Default/Cookies")  # pre-2022 location, no Network/ dir
    assert bc.locate_cookie_store(_browser("Chrome"), "darwin", tmp_path) == store


def test_opera_keeps_its_profile_in_the_root(tmp_path: Path) -> None:
    store = _touch(tmp_path / "Library/Application Support/com.operasoftware.Opera/Cookies")
    assert bc.locate_cookie_store(_browser("Opera"), "darwin", tmp_path) == store


def test_missing_store_says_the_browser_was_never_used(tmp_path: Path) -> None:
    with pytest.raises(ZendriverMCPError, match="No cookie store found for Brave"):
        bc.locate_cookie_store(_browser("Brave"), "darwin", tmp_path)


def test_missing_safari_store_points_at_full_disk_access(tmp_path: Path) -> None:
    with pytest.raises(ZendriverMCPError, match="Full Disk Access"):
        bc.locate_cookie_store(_browser("Safari"), "darwin", tmp_path)


# ---------------------------------------------------------------- domains


def test_normalize_domains_accepts_hosts_dotted_hosts_and_urls() -> None:
    raw = ["GitHub.com", ".github.com", "https://gist.github.com/x?y=1", "  ", "example.org/path"]
    assert bc.normalize_domains(raw) == ["github.com", "gist.github.com", "example.org"]
    assert bc.normalize_domains(None) == []


# ---------------------------------------------------------------- firefox


def _firefox_db(path: Path, rows: list[tuple[Any, ...]]) -> sqlite3.Connection:
    """rows: (originAttributes, name, value, host, path, expiry, isSecure, isHttpOnly, sameSite)"""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE moz_cookies (id INTEGER PRIMARY KEY, originAttributes TEXT NOT NULL "
        "DEFAULT '', name TEXT, value TEXT, host TEXT, path TEXT, expiry INTEGER, "
        "isSecure INTEGER, isHttpOnly INTEGER, sameSite INTEGER)"
    )
    conn.executemany(
        "INSERT INTO moz_cookies (originAttributes, name, value, host, path, expiry, isSecure, "
        "isHttpOnly, sameSite) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return conn


def _row(name: str, host: str, **over: Any) -> tuple[Any, ...]:
    r = {"origin": "", "value": f"v-{name}", "path": "/", "expiry": FUTURE, "secure": 1}
    r.update({"http_only": 0, "same_site": 1})
    r.update(over)
    return (
        r["origin"],
        name,
        r["value"],
        host,
        r["path"],
        r["expiry"],
        r["secure"],
        r["http_only"],
        r["same_site"],
    )


def _read_firefox(tmp_path: Path, rows: list[tuple[Any, ...]], domains: list[str] | None = None):
    store = tmp_path / "cookies.sqlite"
    _firefox_db(store, rows).close()
    return bc.read_cookies(_browser("Zen"), store, domains)


def test_firefox_domain_filter_takes_the_site_and_its_subdomains_only(tmp_path: Path) -> None:
    rows = [
        _row("exact", "github.com"),
        _row("dotted", ".github.com"),
        _row("sub", "gist.github.com"),
        _row("lookalike", "notgithub.com"),
        _row("suffix-trick", "github.com.evil.io"),
        _row("other", "example.org"),
    ]
    names = {c.name for c in _read_firefox(tmp_path, rows, ["github.com"])}
    assert names == {"exact", "dotted", "sub"}


def test_firefox_without_domains_returns_every_site(tmp_path: Path) -> None:
    rows = [_row("a", "github.com"), _row("b", "example.org")]
    assert {c.name for c in _read_firefox(tmp_path, rows)} == {"a", "b"}


def test_firefox_expired_cookies_are_left_behind(tmp_path: Path) -> None:
    rows = [_row("fresh", "a.io"), _row("stale", "a.io", expiry=PAST)]
    assert [c.name for c in _read_firefox(tmp_path, rows)] == ["fresh"]


def test_firefox_millisecond_expiry_is_read_as_the_same_moment(tmp_path: Path) -> None:
    rows = [_row("s", "a.io", expiry=FUTURE), _row("ms", "a.io", expiry=FUTURE * 1000)]
    cookies = {c.name: c for c in _read_firefox(tmp_path, rows)}
    assert cookies["ms"].expires == cookies["s"].expires == FUTURE


def test_firefox_container_and_partitioned_cookies_are_skipped(tmp_path: Path) -> None:
    rows = [
        _row("main", "a.io", value="main-session"),
        _row("main", "a.io", value="work-container", origin="^userContextId=2"),
        _row("third", "a.io", origin="^partitionKey=%28https%2Cb.io%29"),
    ]
    cookies = _read_firefox(tmp_path, rows)
    assert [(c.name, c.value) for c in cookies] == [("main", "main-session")]


@pytest.mark.parametrize(
    ("code", "secure", "expected"),
    [(2, 1, "Strict"), (1, 1, "Lax"), (0, 1, "None"), (0, 0, None), (256, 1, None)],
)
def test_firefox_same_site_mapping(
    tmp_path: Path, code: int, secure: int, expected: str | None
) -> None:
    (cookie,) = _read_firefox(tmp_path, [_row("c", "a.io", same_site=code, secure=secure)])
    assert cookie.same_site == expected


def test_firefox_flags_and_path_carry_over(tmp_path: Path) -> None:
    (cookie,) = _read_firefox(
        tmp_path, [_row("sid", ".a.io", path="/app", secure=1, http_only=1, value="abc")]
    )
    assert cookie == bc.BrowserCookie(
        name="sid",
        value="abc",
        domain=".a.io",
        path="/app",
        secure=True,
        http_only=True,
        same_site="Lax",
        expires=float(FUTURE),
    )


def test_firefox_reads_rows_the_running_browser_has_not_checkpointed(tmp_path: Path) -> None:
    """A live Firefox keeps its newest cookies in the -wal file and holds the db open."""
    store = tmp_path / "cookies.sqlite"
    live = _firefox_db(store, [_row("old", "a.io")])
    live.execute("PRAGMA journal_mode=WAL")
    live.execute("PRAGMA wal_autocheckpoint=0")
    live.execute(
        "INSERT INTO moz_cookies (name, value, host, path, expiry, isSecure, isHttpOnly, sameSite)"
        " VALUES ('new', 'v', 'a.io', '/', ?, 1, 0, 1)",
        (FUTURE,),
    )
    live.commit()
    try:
        assert store.with_name("cookies.sqlite-wal").stat().st_size > 0
        names = {c.name for c in bc.read_cookies(_browser("Zen"), store)}
    finally:
        live.close()
    assert names == {"old", "new"}


def test_unreadable_database_becomes_a_typed_error(tmp_path: Path) -> None:
    store = tmp_path / "cookies.sqlite"
    store.write_bytes(b"this is not sqlite")
    with pytest.raises(ZendriverMCPError, match="Could not read Zen's cookie store"):
        bc.read_cookies(_browser("Zen"), store)


# ---------------------------------------------------------------- chromium


def _key(password: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", iterations, dklen=16)


def _encrypt(value: str, key: bytes, host: str | None = None, prefix: bytes = b"v10") -> bytes:
    plain = value.encode()
    if host is not None:  # schema >= 24 prepends SHA-256(host)
        plain = hashlib.sha256(host.encode()).digest() + plain
    pad = 16 - len(plain) % 16
    plain += bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
    return prefix + encryptor.update(plain) + encryptor.finalize()


def _chromium_expiry(unix_seconds: int) -> int:
    return (unix_seconds + 11_644_473_600) * 1_000_000


def _chromium_db(path: Path, version: int, rows: list[dict[str, Any]]) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta VALUES ('version', ?)", (str(version),))
    conn.execute(
        "CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, encrypted_value BLOB, "
        "path TEXT, expires_utc INTEGER, is_secure INTEGER, is_httponly INTEGER, "
        "samesite INTEGER, has_expires INTEGER)"
    )
    for r in rows:
        conn.execute(
            "INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r["host"],
                r["name"],
                r.get("value", ""),
                r.get("encrypted", b""),
                r.get("path", "/"),
                r.get("expires_utc", _chromium_expiry(FUTURE)),
                r.get("secure", 1),
                r.get("http_only", 0),
                r.get("same_site", -1),
                r.get("has_expires", 1),
            ),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def mac_key(monkeypatch: pytest.MonkeyPatch) -> tuple[bytes, list[str]]:
    """A fake Keychain: returns the key Chrome would derive and records the lookups."""
    asked: list[str] = []

    def fake_keychain(service: str) -> bytes:
        asked.append(service)
        return b"keychain-password"

    monkeypatch.setattr(bc, "_mac_keychain_password", fake_keychain)
    return _key(b"keychain-password", 1003), asked


def test_chromium_decrypts_values_and_strips_the_host_hash(
    tmp_path: Path, mac_key: tuple[bytes, list[str]]
) -> None:
    key, asked = mac_key
    store = tmp_path / "Cookies"
    _chromium_db(
        store,
        24,
        [
            {
                "host": ".github.com",
                "name": "sid",
                "encrypted": _encrypt("s3cr3t", key, ".github.com"),
            }
        ],
    )
    (cookie,) = bc.read_cookies(_browser("Chrome"), store, platform="darwin")
    assert cookie.value == "s3cr3t"
    assert cookie.expires == FUTURE
    assert asked == ["Chrome Safe Storage"]


def test_chromium_before_schema_24_has_no_host_hash_to_strip(
    tmp_path: Path, mac_key: tuple[bytes, list[str]]
) -> None:
    key, _ = mac_key
    store = tmp_path / "Cookies"
    long_value = "x" * 40  # longer than the 32 bytes a wrong strip would eat
    _chromium_db(store, 23, [{"host": "a.io", "name": "c", "encrypted": _encrypt(long_value, key)}])
    (cookie,) = bc.read_cookies(_browser("Chrome"), store, platform="darwin")
    assert cookie.value == long_value


def test_chromium_asks_the_keychain_once_for_many_cookies(
    tmp_path: Path, mac_key: tuple[bytes, list[str]]
) -> None:
    key, asked = mac_key
    store = tmp_path / "Cookies"
    rows = [
        {"host": "a.io", "name": f"c{i}", "encrypted": _encrypt(f"v{i}", key, "a.io")}
        for i in range(3)
    ]
    _chromium_db(store, 24, rows)
    assert len(bc.read_cookies(_browser("Brave"), store, platform="darwin")) == 3
    assert asked == ["Brave Safe Storage"]


def test_chromium_never_touches_the_keychain_when_no_site_matches(
    tmp_path: Path, mac_key: tuple[bytes, list[str]]
) -> None:
    key, asked = mac_key
    store = tmp_path / "Cookies"
    _chromium_db(
        store, 24, [{"host": "bank.example", "name": "c", "encrypted": _encrypt("v", key)}]
    )
    assert bc.read_cookies(_browser("Chrome"), store, ["github.com"], platform="darwin") == []
    assert asked == []


def test_chromium_wrong_key_is_an_error_not_garbage_cookies(
    tmp_path: Path, mac_key: tuple[bytes, list[str]]
) -> None:
    store = tmp_path / "Cookies"
    other_key = _key(b"some-other-password", 1003)
    rows = [
        {"host": "a.io", "name": f"c{i}", "encrypted": _encrypt(f"value-{i}", other_key, "a.io")}
        for i in range(4)
    ]
    _chromium_db(store, 24, rows)
    with pytest.raises(ZendriverMCPError, match="Could not decrypt any of Chrome's cookies"):
        bc.read_cookies(_browser("Chrome"), store, platform="darwin")


def test_chromium_plaintext_and_session_cookies(
    tmp_path: Path, mac_key: tuple[bytes, list[str]]
) -> None:
    _, asked = mac_key
    store = tmp_path / "Cookies"
    _chromium_db(
        store,
        24,
        [
            {"host": "a.io", "name": "plain", "value": "visible", "same_site": 2},
            {"host": "a.io", "name": "session", "value": "s", "has_expires": 0, "expires_utc": 0},
            {"host": "a.io", "name": "stale", "value": "x", "expires_utc": _chromium_expiry(PAST)},
        ],
    )
    cookies = {c.name: c for c in bc.read_cookies(_browser("Chrome"), store, platform="darwin")}
    assert set(cookies) == {"plain", "session"}
    assert cookies["plain"].same_site == "Strict"
    assert cookies["session"].expires is None
    assert asked == []  # nothing was encrypted


def test_chromium_linux_v10_uses_the_fixed_password_without_a_keyring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_keyring(_app: str) -> bytes:
        raise AssertionError("v10 values must not need the secret service")

    monkeypatch.setattr(bc, "_linux_secret", no_keyring)
    store = tmp_path / "Cookies"
    key = _key(b"peanuts", 1)
    _chromium_db(
        store, 24, [{"host": "a.io", "name": "c", "encrypted": _encrypt("v", key, "a.io")}]
    )
    (cookie,) = bc.read_cookies(_browser("Chromium"), store, platform="linux")
    assert cookie.value == "v"


def test_chromium_linux_v11_takes_its_key_from_the_secret_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []

    def secret(app: str) -> bytes:
        asked.append(app)
        return b"from-keyring"

    monkeypatch.setattr(bc, "_linux_secret", secret)
    store = tmp_path / "Cookies"
    key = _key(b"from-keyring", 1)
    _chromium_db(
        store,
        24,
        [{"host": "a.io", "name": "c", "encrypted": _encrypt("v", key, "a.io", prefix=b"v11")}],
    )
    (cookie,) = bc.read_cookies(_browser("Brave"), store, platform="linux")
    assert cookie.value == "v"
    assert asked == ["brave"]


# ---------------------------------------------------------------- safari


def _binarycookie(
    name: str, value: str, domain: str, path: str, flags: int, expires: float
) -> bytes:
    strings = [s.encode() + b"\x00" for s in (domain, name, path, value)]
    offsets, at = [], 56
    for s in strings:
        offsets.append(at)
        at += len(s)
    header = struct.pack("<8I", at, 0, flags, 0, *offsets) + b"\x00" * 8
    return header + struct.pack("<dd", expires, 0.0) + b"".join(strings)


def _binarycookies(pages: list[list[bytes]]) -> bytes:
    built = []
    for cookies in pages:
        head = 8 + 4 * len(cookies) + 4
        offsets, at = [], head
        for c in cookies:
            offsets.append(at)
            at += len(c)
        built.append(
            b"\x00\x00\x01\x00"
            + struct.pack("<I", len(cookies))
            + struct.pack(f"<{len(cookies)}I", *offsets)
            + b"\x00" * 4
            + b"".join(cookies)
        )
    return (
        b"cook"
        + struct.pack(">I", len(built))
        + struct.pack(f">{len(built)}I", *(len(p) for p in built))
        + b"".join(built)
    )


def test_safari_binarycookies_across_pages(tmp_path: Path) -> None:
    apple_future = FUTURE - 978_307_200
    data = _binarycookies(
        [
            [
                _binarycookie("sid", "abc", ".apple.com", "/", 5, apple_future),
                _binarycookie("pref", "dark", "a.io", "/x", 0, apple_future),
            ],
            [_binarycookie("old", "x", "a.io", "/", 1, PAST - 978_307_200)],
        ]
    )
    store = tmp_path / "Cookies.binarycookies"
    store.write_bytes(data)
    cookies = {c.name: c for c in bc.read_cookies(_browser("Safari"), store)}

    assert set(cookies) == {"sid", "pref"}  # the expired one on page 2 is dropped
    assert cookies["sid"] == bc.BrowserCookie(
        name="sid",
        value="abc",
        domain=".apple.com",
        path="/",
        secure=True,
        http_only=True,
        same_site=None,
        expires=float(FUTURE),
    )
    assert (cookies["pref"].secure, cookies["pref"].http_only) == (False, False)


@pytest.mark.parametrize("data", [b"nope" + b"\x00" * 20, _binarycookies([[b"\x00" * 10]])[:-6]])
def test_safari_bad_file_is_a_typed_error(tmp_path: Path, data: bytes) -> None:
    store = tmp_path / "Cookies.binarycookies"
    store.write_bytes(data)
    with pytest.raises(ZendriverMCPError, match="Safari's cookie file"):
        bc.read_cookies(_browser("Safari"), store)


def test_summary_counts_sites_and_never_carries_values() -> None:
    cookies = [
        bc.BrowserCookie("a", "SECRET-1", ".a.io", "/", True, True, None, None),
        bc.BrowserCookie("b", "SECRET-2", "a.io", "/", True, True, None, None),
        bc.BrowserCookie("c", "SECRET-3", "b.io", "/", True, True, None, None),
    ]
    summary = bc.summarize_domains(cookies)
    assert summary == {"sites": 2, "top": {"a.io": 2, "b.io": 1}}
    assert "SECRET" not in repr(summary)
