#!/usr/bin/env python3
"""
Seed script: create admin user and insert models into database.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

import pymysql
from auth import hash_password, generate_api_key
from config import DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME


def main():
    conn = pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        charset="utf8mb4",
    )
    cur = conn.cursor()

    # Create admin user (admin gets a key, regular users start with NULL)
    admin_pw = hash_password("admin123")
    admin_key = generate_api_key()
    try:
        cur.execute(
            "INSERT INTO users (username, email, password_hash, api_key, role, credit) VALUES (%s, %s, %s, %s, 'admin', 1000)",
            ("admin", "admin@orchestraapi.dev", admin_pw, admin_key),
        )
        print(f"[OK] Admin user created")
        print(f"     Username: admin")
        print(f"     Password: admin123")
        print(f"     API Key:  {admin_key}")
    except pymysql.err.IntegrityError:
        print("[SKIP] Admin user already exists")

    # Insert models (prices in USD per 1K tokens)
    # Market price × 2/3 for input; output/input ratio >= 5x → 6× input_sell, else × 2/3
    models = [
        # Claude — market: $5/$25 per 1M → sell: $3.33/$20 per 1M
        ("claude-opus-4.6-fast", "Claude Opus 4.6 Fast", 0.003333, 0.020000),
        ("claude-opus-4.6", "Claude Opus 4.6", 0.003333, 0.020000),
        ("claude-opus-4.5", "Claude Opus 4.5", 0.003333, 0.020000),
        # Claude Sonnet — market: $3/$15 per 1M → sell: $2/$12 per 1M
        ("claude-sonnet-4.6", "Claude Sonnet 4.6", 0.002000, 0.012000),
        ("claude-sonnet-4.5", "Claude Sonnet 4.5", 0.002000, 0.012000),
        ("claude-sonnet-4", "Claude Sonnet 4", 0.002000, 0.012000),
        # Claude Haiku — market: $1/$5 per 1M → sell: $0.667/$4.00 per 1M
        ("claude-haiku-4.5", "Claude Haiku 4.5", 0.000667, 0.004000),
        # Gemini 3.1/3 Pro — market: $2/$12 per 1M → sell: $1.333/$8.00 per 1M
        ("gemini-3.1-pro-preview", "Gemini 3.1 Pro", 0.001333, 0.008000),
        ("gemini-3-pro-preview", "Gemini 3 Pro", 0.001333, 0.008000),
        # Gemini 2.5 Pro — market: $1.25/$10 per 1M → sell: $0.833/$5.00 per 1M
        ("gemini-2.5-pro", "Gemini 2.5 Pro", 0.000833, 0.005000),
        # Gemini 3 Flash — market: $0.50/$3 per 1M → sell: $0.333/$2.00 per 1M
        ("gemini-3-flash-preview", "Gemini 3 Flash", 0.000333, 0.002000),
        # GPT-5.x Codex tier — market: $1.75/$14 per 1M → sell: $1.167/$7.00 per 1M
        ("gpt-5.3-codex", "GPT-5.3 Codex", 0.001167, 0.007000),
        ("gpt-5.2-codex", "GPT-5.2 Codex", 0.001167, 0.007000),
        ("gpt-5.2", "GPT-5.2", 0.001167, 0.007000),
        ("gpt-5.1-codex-max", "GPT-5.1 Codex Max", 0.001167, 0.007000),
        ("gpt-5.4", "GPT-5.4", 0.001167, 0.007000),
        # GPT-5.1 tier — market: $1.25/$10 per 1M → sell: $0.833/$5.00 per 1M
        ("gpt-5.1-codex", "GPT-5.1 Codex", 0.000833, 0.005000),
        ("gpt-5.1", "GPT-5.1", 0.000833, 0.005000),
        # GPT Mini — market: $0.25/$2 per 1M → sell: $0.167/$1.00 per 1M
        ("gpt-5.1-codex-mini", "GPT-5.1 Codex Mini", 0.000167, 0.001000),
        ("gpt-5-mini", "GPT-5 Mini", 0.000167, 0.001000),
        # GPT-4.x Legacy — market: $0.20/$1.50 per 1M → ratio 7.5x → sell: $0.133/$0.80 per 1M
        ("gpt-4o", "GPT-4o", 0.000133, 0.000800),
        ("gpt-4.1", "GPT-4.1", 0.000133, 0.000800),
        ("gpt-4o-mini", "GPT-4o Mini", 0.000100, 0.000400),
        ("gpt-4", "GPT-4", 0.000133, 0.000800),
        ("gpt-3.5-turbo", "GPT-3.5 Turbo", 0.000333, 0.001000),
    ]

    inserted = 0
    for model_id, name, input_price, output_price in models:
        cur.execute(
            """INSERT INTO models (model_id, name, input_price, output_price)
               VALUES (%s, %s, %s, %s)
               ON DUPLICATE KEY UPDATE
                   name = VALUES(name),
                   input_price = VALUES(input_price),
                   output_price = VALUES(output_price)""",
            (model_id, name, input_price, output_price),
        )
        inserted += 1

    conn.commit()
    print(f"[OK] Inserted {inserted} models (skipped duplicates)")
    cur.close()
    conn.close()
    print("[DONE] Seed completed!")


if __name__ == "__main__":
    main()
