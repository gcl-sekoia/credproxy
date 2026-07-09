#!/usr/bin/env python3
"""Read-only reader for a vault unlocked by the Bitwarden CLI (zero-dependency).

Reuses the CLI's own on-disk state (data.json). With BW_SESSION set it decrypts
via the session key (no master password, no KDF); otherwise it falls back to
prompting for the master password and deriving the keys (PBKDF2 vaults only).
Either way it never contacts the server. Sync/edit/login stay the job of the
official `bw` CLI; this only reads.

No third-party packages: AES-256-CBC is vendored (the stdlib has no block
cipher) and RSA-2048 OAEP-SHA1 uses the built-in pow() plus a minimal DER walk.
The vendored AES is correct but NOT constant-time; acceptable for a local
reader of an already-unlocked vault, not for adversarial-timing settings.

Optimized for looking up a few passwords by name in one call: item fields are
decrypted lazily, so a search decrypts only the names (to match) plus the
passwords of the items you actually touch — not every field of every item.

Importable as a library (`Vault`/`Item`) or runnable standalone:
`./pybw.py NAME [NAME ...]` prints name<TAB>password for each match.
"""

import base64
import hashlib
import hmac
import json
import os
import sys


def _xtime(a):
    return ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else a << 1  # x^8+x^4+x^3+x+1


# log/antilog tables over GF(2^8) with generator 3, so multiplication and
# inversion become table lookups instead of bit loops.
_ALOG = [0] * 256
_LOG = [0] * 256
_a = 1
for _i in range(255):
    _ALOG[_i] = _a
    _LOG[_a] = _i
    _a ^= _xtime(_a)  # multiply by 3 = a ^ xtime(a)


def _gmul(a, b):
    if a == 0 or b == 0:
        return 0
    return _ALOG[(_LOG[a] + _LOG[b]) % 255]


def _ginv(a):
    return 0 if a == 0 else _ALOG[(255 - _LOG[a]) % 255]


def _build_sboxes():
    sbox = [0] * 256
    for a in range(256):
        x = _ginv(a)
        s = x
        for _ in range(4):
            x = ((x << 1) | (x >> 7)) & 0xFF
            s ^= x
        sbox[a] = s ^ 0x63  # affine transform
    inv_sbox = [0] * 256
    for i, v in enumerate(sbox):
        inv_sbox[v] = i
    return sbox, inv_sbox


_SBOX, _INV_SBOX = _build_sboxes()

# Precomputed products for the four InvMixColumns coefficients.
_M9 = [_gmul(9, x) for x in range(256)]
_M11 = [_gmul(11, x) for x in range(256)]
_M13 = [_gmul(13, x) for x in range(256)]
_M14 = [_gmul(14, x) for x in range(256)]


def _expand_key(key):
    assert len(key) == 32, "AES-256 key must be 32 bytes"
    nk, nr = 8, 14
    words = [list(key[4 * i : 4 * i + 4]) for i in range(nk)]
    rcon = 1
    for i in range(nk, 4 * (nr + 1)):
        temp = list(words[i - 1])
        if i % nk == 0:
            temp = temp[1:] + temp[:1]  # RotWord
            temp = [_SBOX[b] for b in temp]
            temp[0] ^= rcon
            rcon = _gmul(rcon, 2)
        elif i % nk == 4:
            temp = [_SBOX[b] for b in temp]
        words.append([words[i - nk][j] ^ temp[j] for j in range(4)])
    return [sum(words[4 * r : 4 * r + 4], []) for r in range(nr + 1)]


def _inv_shift_rows(s):
    # state byte i holds row i%4, column i//4; row r rotates right by r
    out = [0] * 16
    for r in range(4):
        for c in range(4):
            out[r + 4 * ((c + r) % 4)] = s[r + 4 * c]
    return out


def _inv_mix_columns(s):
    out = [0] * 16
    for c in range(4):
        a0, a1, a2, a3 = s[4 * c : 4 * c + 4]
        out[4 * c + 0] = _M14[a0] ^ _M11[a1] ^ _M13[a2] ^ _M9[a3]
        out[4 * c + 1] = _M9[a0] ^ _M14[a1] ^ _M11[a2] ^ _M13[a3]
        out[4 * c + 2] = _M13[a0] ^ _M9[a1] ^ _M14[a2] ^ _M11[a3]
        out[4 * c + 3] = _M11[a0] ^ _M13[a1] ^ _M9[a2] ^ _M14[a3]
    return out


def _decrypt_block(block, round_keys):
    nr = len(round_keys) - 1
    s = [block[i] ^ round_keys[nr][i] for i in range(16)]
    for rnd in range(nr - 1, 0, -1):
        s = _inv_shift_rows(s)
        s = [_INV_SBOX[b] for b in s]
        s = [s[i] ^ round_keys[rnd][i] for i in range(16)]
        s = _inv_mix_columns(s)
    s = _inv_shift_rows(s)
    s = [_INV_SBOX[b] for b in s]
    return bytes(s[i] ^ round_keys[0][i] for i in range(16))


def _aes256_cbc_decrypt(key, iv, ciphertext):
    round_keys = _expand_key(key)
    out = bytearray()
    prev = iv
    for off in range(0, len(ciphertext), 16):
        block = ciphertext[off : off + 16]
        dec = _decrypt_block(block, round_keys)
        out += bytes(dec[i] ^ prev[i] for i in range(16))
        prev = block
    return bytes(out)


def _read_der_tlv(buf, pos):
    tag = buf[pos]
    pos += 1
    length = buf[pos]
    pos += 1
    if length & 0x80:
        n = length & 0x7F
        length = int.from_bytes(buf[pos : pos + n], "big")
        pos += n
    return tag, buf[pos : pos + length], pos + length


def _parse_pkcs8_rsa(der):
    """Extract (n, d) from a PKCS#8 PrivateKeyInfo wrapping an RSAPrivateKey."""
    _, pki, _ = _read_der_tlv(der, 0)  # outer SEQUENCE
    pos = 0
    _, _, pos = _read_der_tlv(pki, pos)  # version INTEGER
    _, _, pos = _read_der_tlv(pki, pos)  # algorithm SEQUENCE
    _, inner, _ = _read_der_tlv(pki, pos)  # privateKey OCTET STRING
    _, rsa, _ = _read_der_tlv(inner, 0)  # RSAPrivateKey SEQUENCE
    p = 0
    _, _, p = _read_der_tlv(rsa, p)  # version
    _, n_b, p = _read_der_tlv(rsa, p)  # modulus
    _, _, p = _read_der_tlv(rsa, p)  # publicExponent
    _, d_b, p = _read_der_tlv(rsa, p)  # privateExponent
    return int.from_bytes(n_b, "big"), int.from_bytes(d_b, "big")


def _mgf1_sha1(seed, length):
    out = b""
    counter = 0
    while len(out) < length:
        out += hashlib.sha1(seed + counter.to_bytes(4, "big")).digest()
        counter += 1
    return out[:length]


def _rsa_oaep_sha1_decrypt(der_private_key, ciphertext):
    n, d = _parse_pkcs8_rsa(der_private_key)
    k = (n.bit_length() + 7) // 8
    m = pow(int.from_bytes(ciphertext, "big"), d, n)
    em = m.to_bytes(k, "big")

    hlen = 20
    lhash = hashlib.sha1(b"").digest()
    masked_seed = em[1 : 1 + hlen]
    masked_db = em[1 + hlen :]
    seed = bytes(a ^ b for a, b in zip(masked_seed, _mgf1_sha1(masked_db, hlen)))
    db = bytes(a ^ b for a, b in zip(masked_db, _mgf1_sha1(seed, k - 1 - hlen)))
    if db[:hlen] != lhash:
        raise ValueError("OAEP lHash mismatch")
    i = hlen
    while i < len(db) and db[i] == 0:
        i += 1
    if i == len(db) or db[i] != 0x01:
        raise ValueError("OAEP padding: separator not found")
    return db[i + 1 :]


def _unpad(b):
    return b[: -b[-1]]


def _aes_cbc_hmac_decrypt(iv, ct, mac, enc_key, mac_key):
    # Bitwarden type 2: encrypt-then-MAC, HMAC-SHA256 over iv||ct, verified first.
    expected = hmac.new(mac_key, iv + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, mac):
        raise ValueError("MAC verification failed")
    return _unpad(_aes256_cbc_decrypt(enc_key, iv, ct))


def _decrypt_encstring(s, enc_key, mac_key):
    ty, rest = s.split(".", 1)
    if ty != "2":
        raise ValueError(f"unsupported symmetric EncString type {ty}")
    iv_b, ct_b, mac_b = rest.split("|")
    return _aes_cbc_hmac_decrypt(
        base64.b64decode(iv_b),
        base64.b64decode(ct_b),
        base64.b64decode(mac_b),
        enc_key,
        mac_key,
    )


def _decrypt_encarraybuffer(b64, enc_key, mac_key):
    # Secure-storage serialization: [type:1][iv:16][mac:32][ciphertext].
    raw = base64.b64decode(b64)
    if raw[0] != 2:
        raise ValueError(f"unsupported EncArrayBuffer type {raw[0]}")
    return _aes_cbc_hmac_decrypt(raw[1:17], raw[49:], raw[17:49], enc_key, mac_key)


def _decrypt_rsa(s, private_key_der):
    ty, rest = s.split(".", 1)
    if ty != "4":
        raise ValueError(f"unsupported asymmetric EncString type {ty}")
    ct = base64.b64decode(rest.split("|")[0])
    # Bitwarden type 4 is RSA-2048-OAEP-SHA1 (SHA1 for both MGF1 and label hash).
    return _rsa_oaep_sha1_decrypt(private_key_der, ct)


def _hkdf_expand_sha256(prk, info, length):
    # RFC 5869 expand: Bitwarden stretches the 32-byte master key into the
    # 64-byte (enc||mac) key that wraps the user key.
    out = b""
    block = b""
    counter = 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def _derive_master_key(password, email, kdf):
    if kdf["kdfType"] != 0:
        raise RuntimeError(
            f"unsupported kdfType {kdf['kdfType']} (only PBKDF2 is implemented)"
        )
    # PBKDF2-HMAC-SHA256 with the account email as salt.
    return hashlib.pbkdf2_hmac("sha256", password, email.encode(), kdf["iterations"], 32)


def _user_key_from_password(data, uid, password):
    kdf = data[f"user_{uid}_kdfConfig_kdfConfig"]
    email = data[f"user_{uid}_masterPasswordUnlock_masterPasswordUnlockKey"]["salt"]
    master_key = _derive_master_key(password, email, kdf)
    stretched = _hkdf_expand_sha256(master_key, b"enc", 32) + _hkdf_expand_sha256(
        master_key, b"mac", 32
    )
    try:
        return _decrypt_encstring(
            data[f"user_{uid}_masterPassword_masterKeyEncryptedUserKey"],
            stretched[:32],
            stretched[32:],
        )
    except ValueError:
        raise RuntimeError("incorrect master password") from None


class Item:
    """A vault entry whose fields decrypt on access (per-item key derived once)."""

    __slots__ = ("_vault", "_cipher", "_keys")

    def __init__(self, vault, cipher):
        self._vault = vault
        self._cipher = cipher
        self._keys = None

    def _dec(self, s):
        if s is None:
            return None
        if self._keys is None:
            self._keys = self._vault._cipher_keys(self._cipher)
        return self._vault._sym_decrypt(s, self._keys).decode()

    @property
    def id(self):
        return self._cipher["id"]

    @property
    def type(self):
        return self._cipher["type"]

    @property
    def org(self):
        return self._cipher.get("organizationId")

    @property
    def name(self):
        return self._dec(self._cipher.get("name"))

    def _login(self, field):
        return self._dec((self._cipher.get("login") or {}).get(field))

    @property
    def username(self):
        return self._login("username")

    @property
    def password(self):
        return self._login("password")

    @property
    def totp(self):
        return self._login("totp")

    @property
    def uri(self):
        uris = (self._cipher.get("login") or {}).get("uris") or []
        return self._dec(uris[0].get("uri")) if uris else None

    @property
    def notes(self):
        return self._dec(self._cipher.get("notes"))

    def field(self, name):
        """A custom field's value by (plaintext) name. Field names are encrypted
        on disk, so decrypt each to match -- mirroring how the CLI matches the
        decrypted `fields[].name`."""
        for f in self._cipher.get("fields") or []:
            if self._dec(f.get("name")) == name:
                return self._dec(f.get("value")) or ""
        return None


class Vault:
    def __init__(self, data, user_key):
        self._data = data
        uid = data["global_account_activeAccountId"]
        self._uid = uid
        self._user = (user_key[:32], user_key[32:])

        priv_der = self._sym_decrypt(data[f"user_{uid}_crypto_privateKey"], self._user)

        self._org = {}
        for org_id, entry in data.get(
            f"user_{uid}_crypto_organizationKeys", {}
        ).items():
            org_key = _decrypt_rsa(entry["key"], priv_der)
            self._org[org_id] = (org_key[:32], org_key[32:])

    @classmethod
    def from_session(cls, data, session_b64):
        uid = data["global_account_activeAccountId"]
        session = base64.b64decode(session_b64)
        user_key = _decrypt_encarraybuffer(
            data[f"__PROTECTED__{uid}_user_auto"], session[:32], session[32:]
        )
        return cls(data, user_key)

    @classmethod
    def from_password(cls, data, password):
        if isinstance(password, str):
            password = password.encode()
        uid = data["global_account_activeAccountId"]
        return cls(data, _user_key_from_password(data, uid, password))

    @classmethod
    def open(cls, password=None):
        data = read_appdata()
        session = os.environ.get("BW_SESSION")
        if session:
            try:
                return cls.from_session(data, session)
            except Exception as e:
                # session present but unusable (stale after a lock/timeout,
                # rotated, or the stored key changed) — fall back to password
                print(
                    f"BW_SESSION set but unusable ({e}); "
                    "falling back to master password",
                    file=sys.stderr,
                )
        if password is None:
            import getpass

            password = getpass.getpass("Master password: ")
        return cls.from_password(data, password)

    def _sym_decrypt(self, s, keys):
        return _decrypt_encstring(s, keys[0], keys[1])

    def _cipher_keys(self, cipher):
        keys = self._org[cipher["organizationId"]] if cipher.get(
            "organizationId"
        ) else self._user
        if cipher.get("key"):
            item_key = self._sym_decrypt(cipher["key"], keys)
            return (item_key[:32], item_key[32:])
        return keys

    def items(self):
        ciphers = self._data[f"user_{self._uid}_ciphers_ciphers"]
        for cipher in ciphers.values():
            if cipher.get("deletedDate"):
                continue
            yield Item(self, cipher)

    def find(self, *names, fold_case=True):
        """Yield items whose exact name matches any of `names`.

        Only names are decrypted during the scan; callers decrypt passwords (or
        other fields) lazily on the returned items.
        """
        wanted = {n.lower() for n in names} if fold_case else set(names)
        for item in self.items():
            name = item.name
            if name is not None and (name.lower() if fold_case else name) in wanted:
                yield item


def _appdata_dir():
    override = os.environ.get("BITWARDENCLI_APPDATA_DIR")
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".config", "Bitwarden CLI")


def read_appdata():
    """Parse the Bitwarden CLI's on-disk state (data.json) from the appdata dir."""
    with open(os.path.join(_appdata_dir(), "data.json")) as fh:
        return json.load(fh)


def kdf_type(data):
    """The active account's KDF type: 0 = PBKDF2 (the pure master-password path
    can derive keys), non-0 = Argon2id etc. (it can't — needs a session or the
    bw CLI). Lets a caller avoid prompting for a password it couldn't use."""
    uid = data["global_account_activeAccountId"]
    return data[f"user_{uid}_kdfConfig_kdfConfig"].get("kdfType")


def main(argv):
    names = argv[1:]
    if not names:
        sys.exit("usage: pybw.py NAME [NAME ...]  (prints name<TAB>password)")
    vault = Vault.open()
    found = set()
    for item in vault.find(*names):
        found.add(item.name.lower())
        print(f"{item.name}\t{item.password}")
    for n in names:
        if n.lower() not in found:
            print(f"warning: no item named {n!r}", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv)
