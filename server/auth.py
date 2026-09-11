"""Passwords, sessions and the node token.

Deliberately small and dependency-free: hashing comes from hashlib and
randomness from secrets, both standard library, so there's nothing here
to keep updated and nothing that can fail to install on a NAS.

The shape of it:

  * One admin account. Forge is a tool for the person who runs it, not a
    multi-user service, and inventing roles nobody asked for would add
    a lot of surface for no benefit.
  * Browsers get a session cookie. Workers get a token instead, because
    a worker can't log in and shouldn't hold the admin password.
  * Nothing is enforced until a password is actually set, so updating
    the server doesn't lock anyone out of their own queue.
"""

import base64
import hashlib
import hmac
import secrets
import time

# scrypt, sized so a single guess costs real time and memory on the
# attacker's machine while staying comfortable on a NAS: about 16MB and
# a few tens of milliseconds per attempt.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
KEY_BYTES = 32
SALT_BYTES = 16

SESSION_DAYS = 30

# Guessing is slowed by making attempts wait, never by locking the
# account. A hard lockout would be a way to keep the owner out of their
# own queue, and that's the thing this is supposed to protect.
BACKOFF_AFTER = 3          # fumbles that cost nothing
BACKOFF_CAP = 30           # seconds between attempts, at worst
ATTEMPT_WINDOW = 15 * 60   # quiet for this long and the count resets


def hash_password(password):
    """A self-describing hash string, safe to store as-is.

    The parameters travel with the hash so they can be raised later
    without stranding existing passwords — an old hash still says how it
    was made, and still verifies.
    """
    salt = secrets.token_bytes(SALT_BYTES)
    key = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N,
                         r=SCRYPT_R, p=SCRYPT_P, dklen=KEY_BYTES,
                         maxmem=64 * 1024 * 1024)
    return "$".join(["scrypt", str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P),
                     base64.b64encode(salt).decode(),
                     base64.b64encode(key).decode()])


def verify_password(password, stored):
    """True if this password made that hash. Never raises."""
    if not password or not stored:
        return False
    try:
        scheme, n, r, p, salt_b64, key_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(key_b64)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                                n=int(n), r=int(r), p=int(p),
                                dklen=len(expected),
                                maxmem=64 * 1024 * 1024)
    except (ValueError, TypeError, MemoryError):
        return False
    # Compared in constant time: a plain == leaks how much of the hash
    # matched through how long it took to say no.
    return hmac.compare_digest(actual, expected)


def new_token():
    """A fresh secret for a session cookie or a node."""
    return secrets.token_urlsafe(32)


def same(a, b):
    """Constant-time comparison for tokens that arrive from outside."""
    return hmac.compare_digest(str(a or ""), str(b or ""))


class Attempts:
    """Throttles password guessing.

    Counted globally rather than per client, which is the opposite of
    the usual advice and deliberate here. Forge normally sits behind a
    reverse proxy, so either every request appears to come from the
    proxy — making per-client buckets meaningless — or the client is
    read from X-Forwarded-For, which the client itself sets and can
    change on every request to get a fresh bucket. A global count can't
    be dodged that way.

    What that costs is that someone hammering the login slows the owner
    down too. So it's a delay that tops out in seconds, not a lockout:
    an attacker gets a couple of guesses a minute, and the owner who
    mistyped waits a moment and carries on.
    """

    def __init__(self):
        self.failures = 0
        self.last = 0.0

    def _expired(self, now):
        return self.failures and now - self.last > ATTEMPT_WINDOW

    def blocked_for(self, _client=None):
        """Seconds to wait before another attempt is worth making."""
        now = time.time()
        if self._expired(now):
            self.failures, self.last = 0, 0.0
            return 0
        if self.failures <= BACKOFF_AFTER:
            return 0
        delay = min(2 ** (self.failures - BACKOFF_AFTER), BACKOFF_CAP)
        remaining = delay - (now - self.last)
        return int(remaining) + 1 if remaining > 0 else 0

    def record_failure(self, _client=None):
        now = time.time()
        if self._expired(now):
            self.failures = 0
        self.failures += 1
        self.last = now

    def clear(self, _client=None):
        self.failures, self.last = 0, 0.0
