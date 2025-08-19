"""
Utility functions for the Valera bot.
"""
from __future__ import annotations

import base64
import random


def generate_referral_code(user_id: int) -> str:
    """Generate a simple referral code based on the user ID and a random suffix."""
    # Use user_id encoded in base32 with a random suffix for obfuscation
    base = base64.b32encode(str(user_id).encode()).decode().rstrip("=")
    suffix = ''.join(random.choices('ABCDEFGHJKLMNPQRSTUVWXYZ23456789', k=4))
    return f"{base}{suffix}"
