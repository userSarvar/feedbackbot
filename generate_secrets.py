"""
 Run this once locally to generate your FERNET_KEY and WEBHOOK_SECRET.
Copy the output into Railway environment variables.

Usage:
    python generate_secrets.py
"""
import secrets
from cryptography.fernet import Fernet

fernet_key     = Fernet.generate_key().decode()
webhook_secret = secrets.token_urlsafe(32)

print("=" * 60)
print("Copy these into Railway → Variables:")
print("=" * 60)
print(f"FERNET_KEY={fernet_key}")
print(f"WEBHOOK_SECRET={webhook_secret}")
print("=" * 60)
print("Also add:")
print("BUILDER_BOT_TOKEN=<your builder bot token from BotFather>")
print("PUBLIC_URL=<your Railway public URL, set after deploy>")
print("=" * 60)
