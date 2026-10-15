import unittest
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from webprint.app import (
    RateLimiter,
    verify_admin_token,
    ACTIVE_TOKENS,
    hash_pin,
    generate_password_hash,
    check_password_hash
)

class TestAuthAndRateLimiting(unittest.TestCase):
    def setUp(self):
        ACTIVE_TOKENS.clear()

    def test_rate_limiter_allows_up_to_limit(self):
        limiter = RateLimiter(max_requests=5, window_seconds=60)
        ip = "192.168.1.50"
        for _ in range(5):
            self.assertTrue(limiter.is_allowed(ip))
        # 6th attempt should be blocked
        self.assertFalse(limiter.is_allowed(ip))

        # Different IP should still be allowed
        self.assertTrue(limiter.is_allowed("192.168.1.51"))

    def test_rate_limiter_reset(self):
        limiter = RateLimiter(max_requests=2, window_seconds=60)
        ip = "10.0.0.99"
        self.assertTrue(limiter.is_allowed(ip))
        self.assertTrue(limiter.is_allowed(ip))
        self.assertFalse(limiter.is_allowed(ip))

        limiter.reset(ip)
        self.assertTrue(limiter.is_allowed(ip))

    def test_verify_admin_token_valid_and_expired(self):
        token = "test_valid_token_12345"
        ACTIVE_TOKENS[token] = time.time() + 300
        self.assertTrue(verify_admin_token(token))

        # Expired token
        expired_token = "test_expired_token_67890"
        ACTIVE_TOKENS[expired_token] = time.time() - 10
        self.assertFalse(verify_admin_token(expired_token))

        # Invalid token
        self.assertFalse(verify_admin_token("non_existent_token"))
        self.assertFalse(verify_admin_token(""))
        self.assertFalse(verify_admin_token(None))

    def test_pin_hmac_hashing(self):
        pin = "123456"
        h1 = hash_pin(pin)
        h2 = hash_pin(pin)
        self.assertEqual(h1, h2)
        self.assertNotEqual(h1, hash_pin("654321"))
        self.assertEqual(len(h1), 64)  # SHA-256 hex length

    def test_password_hash_scrypt(self):
        pwd = "SecureAppliancePassword123!"
        h = generate_password_hash(pwd, method='scrypt')
        self.assertTrue(check_password_hash(h, pwd))
        self.assertFalse(check_password_hash(h, "WrongPassword"))

if __name__ == '__main__':
    unittest.main()
