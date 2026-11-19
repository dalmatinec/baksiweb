"""Подписи Telegram (Mini App, Login Widget), шифрование токенов, CryptoBot-вебхук."""
import hashlib
import hmac
import json
import sys
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException  # noqa: E402

from baksi.crypto import Vault, mask_token  # noqa: E402
from baksi.cryptopay import CryptoPay  # noqa: E402
from baksi.web.security import check_login_widget, check_webapp_init_data  # noqa: E402

TOKEN = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"


def sign_webapp(data: dict) -> str:
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    return urlencode(data | {"hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()})


class SecurityTests(unittest.TestCase):
    def test_webapp_init_data(self):
        data = {"auth_date": str(int(time.time())), "user": json.dumps({"id": 42, "first_name": "A"})}
        self.assertEqual(check_webapp_init_data(sign_webapp(data), TOKEN)["id"], 42)
        forged = sign_webapp(data).replace("42", "43")
        with self.assertRaises(HTTPException):
            check_webapp_init_data(forged, TOKEN)
        old = {"auth_date": str(int(time.time()) - 3 * 86400), "user": json.dumps({"id": 42})}
        with self.assertRaises(HTTPException):
            check_webapp_init_data(sign_webapp(old), TOKEN)

    def test_login_widget(self):
        data = {"id": "7", "first_name": "B", "auth_date": str(int(time.time()))}
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        h = hmac.new(hashlib.sha256(TOKEN.encode()).digest(), check.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(check_login_widget(data | {"hash": h}, TOKEN)["id"], 7)
        with self.assertRaises(HTTPException):
            check_login_widget(data | {"hash": "0" * 64}, TOKEN)

    def test_vault(self):
        v = Vault("x" * 40)
        blob = v.encrypt(TOKEN)
        self.assertNotIn("ABCDEF", blob)
        self.assertEqual(v.decrypt(blob), TOKEN)
        self.assertIsNone(Vault("y" * 40).decrypt(blob), "чужой ключ не расшифрует")
        self.assertEqual(mask_token(TOKEN), "123456:••••••••fghi")

    def test_cryptopay_signature(self):
        cp = CryptoPay("cp-token", False, None, None, "B")
        body = b'{"update_type":"invoice_paid"}'
        sig = hmac.new(hashlib.sha256(b"cp-token").digest(), body, hashlib.sha256).hexdigest()
        self.assertTrue(cp.verify(body, sig))
        self.assertFalse(cp.verify(body, "bad"))
        self.assertFalse(cp.verify(body + b" ", sig))


if __name__ == "__main__":
    unittest.main()
