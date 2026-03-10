"""Integration test: starts backend server, runs all tests, then stops."""
import subprocess
import sys
import time

try:
    import httpx
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "httpx", "-q"])
    import httpx

API = "http://localhost:8080"


def wait_for_server(url, timeout=10):
    for _ in range(timeout * 10):
        try:
            r = httpx.get(url + "/api/models", timeout=2)
            if r.status_code == 200:
                return True
        except:
            pass
        time.sleep(0.1)
    return False


def main():
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"],
        cwd="/home/dangnosuy/Documents/tool/copilot-api-server/server/backend",
        env={
            **__import__("os").environ,
            "PYTHONPATH": "/home/dangnosuy/Documents/tool/copilot-api-server/server/backend"
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        if not wait_for_server(API):
            print("FAIL: Server did not start")
            return 1

        passed = 0
        failed = 0

        def test(name, check):
            nonlocal passed, failed
            try:
                ok = check()
                if ok:
                    print(f"  PASS: {name}")
                    passed += 1
                else:
                    print(f"  FAIL: {name}")
                    failed += 1
            except Exception as e:
                print(f"  FAIL: {name} -> {e}")
                failed += 1

        # ========== AUTH ==========
        print("\n=== AUTH ===")

        test_user = f"testuser_{int(time.time())}"
        test_email = f"{test_user}@test.com"
        test_pass = "testpass123"

        def test_register():
            r = httpx.post(f"{API}/api/auth/register", json={
                "username": test_user, "email": test_email, "password": test_pass
            })
            data = r.json()
            # api_key should be None on registration
            return r.status_code == 200 and "token" in data and data["user"]["api_key"] is None
        test("Register (api_key=NULL)", test_register)

        def test_dup_register():
            r = httpx.post(f"{API}/api/auth/register", json={
                "username": test_user, "email": test_email, "password": test_pass
            })
            return r.status_code == 400
        test("Duplicate register blocked", test_dup_register)

        user_token = None
        def test_login():
            nonlocal user_token
            r = httpx.post(f"{API}/api/auth/login", json={"login": test_user, "password": test_pass})
            data = r.json()
            user_token = data.get("token")
            return r.status_code == 200 and user_token is not None
        test("Login with username", test_login)

        def test_login_email():
            r = httpx.post(f"{API}/api/auth/login", json={"login": test_email, "password": test_pass})
            return r.status_code == 200
        test("Login with email", test_login_email)

        def test_wrong_pass():
            r = httpx.post(f"{API}/api/auth/login", json={"login": test_user, "password": "wrong"})
            return r.status_code == 401
        test("Wrong password rejected", test_wrong_pass)

        # ========== USER ==========
        print("\n=== USER ===")
        headers = {"Authorization": f"Bearer {user_token}"}

        def test_profile():
            r = httpx.get(f"{API}/api/user/profile", headers=headers)
            data = r.json()
            return r.status_code == 200 and data["username"] == test_user and data["api_key"] is None
        test("Get profile (api_key=NULL)", test_profile)

        new_name = f"renamed_{int(time.time())}"
        def test_update_profile():
            r = httpx.put(f"{API}/api/user/profile", headers=headers, json={"username": new_name})
            return r.status_code == 200
        test("Update username", test_update_profile)

        def test_profile_updated():
            r = httpx.get(f"{API}/api/user/profile", headers=headers)
            return r.json()["username"] == new_name
        test("Profile reflects new name", test_profile_updated)

        def test_gen_key():
            r = httpx.post(f"{API}/api/user/regenerate-key", headers=headers)
            data = r.json()
            return r.status_code == 200 and data["api_key"].startswith("oct-")
        test("Generate API key (oct- prefix)", test_gen_key)

        def test_key_persisted():
            r = httpx.get(f"{API}/api/user/profile", headers=headers)
            data = r.json()
            return data["api_key"] is not None and data["api_key"].startswith("oct-")
        test("API key persisted in profile", test_key_persisted)

        def test_credits():
            r = httpx.get(f"{API}/api/user/credits", headers=headers)
            return r.status_code == 200 and "credit" in r.json()
        test("Get credits", test_credits)

        def test_usage():
            r = httpx.get(f"{API}/api/user/usage", headers=headers)
            return r.status_code == 200
        test("Get usage", test_usage)

        # ========== MODELS ==========
        print("\n=== MODELS ===")

        def test_models():
            r = httpx.get(f"{API}/api/models")
            data = r.json()
            models = data.get("models", data) if isinstance(data, dict) else data
            return r.status_code == 200 and len(models) >= 20
        test("Get models (public)", test_models)

        # ========== ADMIN ==========
        print("\n=== ADMIN ===")

        admin_token = None
        def test_admin_login():
            nonlocal admin_token
            r = httpx.post(f"{API}/api/auth/login", json={"login": "admin", "password": "admin123"})
            data = r.json()
            admin_token = data.get("token")
            return r.status_code == 200 and data["user"]["role"] == "admin"
        test("Admin login", test_admin_login)

        admin_h = {"Authorization": f"Bearer {admin_token}"}

        def test_admin_stats():
            r = httpx.get(f"{API}/api/admin/stats", headers=admin_h)
            return r.status_code == 200 and "total_users" in r.json()
        test("Admin stats", test_admin_stats)

        def test_admin_users():
            r = httpx.get(f"{API}/api/admin/users", headers=admin_h)
            data = r.json()
            users = data.get("users", data) if isinstance(data, dict) else data
            return r.status_code == 200 and len(users) >= 2
        test("Admin list users", test_admin_users)

        def test_admin_models():
            r = httpx.get(f"{API}/api/admin/models", headers=admin_h)
            data = r.json()
            models = data.get("models", data) if isinstance(data, dict) else data
            return r.status_code == 200 and len(models) >= 20
        test("Admin list models", test_admin_models)

        def test_admin_usage():
            r = httpx.get(f"{API}/api/admin/usage", headers=admin_h)
            return r.status_code == 200
        test("Admin usage", test_admin_usage)

        def test_toggle_model():
            data = httpx.get(f"{API}/api/admin/models", headers=admin_h).json()
            models = data.get("models", data) if isinstance(data, dict) else data
            model_id = models[0]["model_id"]
            current_active = models[0]["is_active"]
            r = httpx.patch(f"{API}/api/admin/models/{model_id}/toggle", headers=admin_h,
                           json={"is_active": not current_active})
            httpx.patch(f"{API}/api/admin/models/{model_id}/toggle", headers=admin_h,
                       json={"is_active": current_active})
            return r.status_code == 200 and "message" in r.json()
        test("Toggle model active", test_toggle_model)

        def test_admin_charts():
            r = httpx.get(f"{API}/api/admin/charts", headers=admin_h)
            data = r.json()
            return r.status_code == 200 and "daily_usage" in data and "model_distribution" in data and "hourly_requests" in data
        test("Admin charts endpoint", test_admin_charts)

        def test_non_admin_blocked():
            r = httpx.get(f"{API}/api/admin/stats", headers=headers)
            return r.status_code == 403
        test("Non-admin blocked from admin", test_non_admin_blocked)

        # ========== SUMMARY ==========
        print(f"\n{'='*40}")
        print(f"Total: {passed + failed} | Passed: {passed} | Failed: {failed}")
        print(f"{'='*40}")
        return 0 if failed == 0 else 1

    finally:
        proc.terminate()
        proc.wait(timeout=5)


if __name__ == "__main__":
    sys.exit(main())
