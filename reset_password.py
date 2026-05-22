"""
Reset the admin password for a team.
Usage:  python reset_password.py <team>
   or:  python reset_password.py <team> <newpassword>

If no password is given, a random one is generated.
The admin will be required to change it on next login.
"""
import sys, json, sqlite3, os, secrets

if len(sys.argv) < 2:
    print("Usage: python reset_password.py <team> [newpassword]")
    print("Example: python reset_password.py demo")
    print("Example: python reset_password.py demo mynewpassword")
    sys.exit(1)

TEAM = sys.argv[1].strip().lower()
NEW_PASSWORD = sys.argv[2] if len(sys.argv) > 2 else secrets.token_urlsafe(12)
DB = f"/data/tenants/{TEAM}/roadmap.db"

if not os.path.exists(DB):
    print(f"ERROR: Database not found at {DB}")
    print(f"Available teams:")
    tenants_dir = "/data/tenants"
    if os.path.isdir(tenants_dir):
        for d in sorted(os.listdir(tenants_dir)):
            if os.path.isfile(os.path.join(tenants_dir, d, "roadmap.db")):
                print(f"  - {d}")
    sys.exit(1)

# Use the same hashing logic as server.py
try:
    import bcrypt
    hashed = bcrypt.hashpw(NEW_PASSWORD.encode(), bcrypt.gensalt(12)).decode()
    method = "bcrypt"
except ImportError:
    import hashlib, hmac
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", NEW_PASSWORD.encode(), salt.encode(), 260000).hex()
    hashed = f"$pbkdf2${salt}${h}"
    method = "pbkdf2"

con = sqlite3.connect(DB)
row = con.execute("SELECT value FROM config WHERE key='users'").fetchone()
if not row:
    print("ERROR: No users found in database.")
    con.close()
    sys.exit(1)

users = json.loads(row[0])
admin = next((u for u in users if u.get("role") == "admin"), None)
if not admin:
    admin = next((u for u in users if u.get("builtin")), None)
if not admin:
    print("ERROR: No admin user found.")
    con.close()
    sys.exit(1)

admin["password"] = hashed
admin["mustChangePassword"] = True

con.execute("UPDATE config SET value=? WHERE key='users'", (json.dumps(users),))
con.commit()
con.close()

print(f"")
print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
print(f"  Password Reset — {TEAM}")
print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
print(f"  Username:  {admin['username']}")
print(f"  Password:  {NEW_PASSWORD}")
print(f"  Hash:      {method}")
print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
print(f"")
print(f"⚠  Password change will be required on next login.")
print(f"")
