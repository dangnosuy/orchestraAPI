"""
Backend API Configuration
"""
import os

# MySQL
DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = int(os.environ.get("DB_PORT", 3306))
DB_USER = os.environ.get("DB_USER", "githubcopilot")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "ghcplserver")
DB_NAME = os.environ.get("DB_NAME", "api_gateway_db")

# JWT
JWT_SECRET = os.environ.get("JWT_SECRET", "orchestraapi_jwt_secret_key_change_in_production_2026")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_DAYS = 2

# Server
BACKEND_PORT = int(os.environ.get("BACKEND_PORT", 8080))
API_SERVER_URL = os.environ.get("API_SERVER_URL", "http://localhost:5000")

# API Key prefix
API_KEY_PREFIX = "oct-"

# PayPal
PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "AVbSbSirMQnRaLVH4-Szgl0c3fVQ-6-LVYMhjPxfy3mSh0vpw-0yyHea1iae6vwk7bW0Hnu1hUd8aERA")
PAYPAL_SECRET = os.environ.get("PAYPAL_SECRET", "EFEnR457OA6ezO6yfJUx3uwpsVYxpqNbW1BDypzfTFvij0OzgVsN56oV4umFeFyqSA6TEvD7k_XG80FE")
PAYPAL_MODE = os.environ.get("PAYPAL_MODE", "sandbox")  # sandbox | live
PAYPAL_WEBHOOK_ID = os.environ.get("PAYPAL_WEBHOOK_ID", "55G71130XF5883929")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000")
