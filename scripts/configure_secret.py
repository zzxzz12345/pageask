"""Save a local-only key without putting it in command arguments or terminal echo."""
import getpass
import os
from pathlib import Path

target = Path(__file__).resolve().parent.parent / ".env.local"
secret = getpass.getpass("Temporary provider key (hidden): ").strip()
if not secret or "\n" in secret or "\r" in secret:
    raise SystemExit("A single nonempty key is required")
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
os.fchmod(fd, 0o600)
with os.fdopen(fd, "w") as handle:
    handle.write("COMMANDCODE_API_KEY=" + secret + "\n")
print("Saved local backend credential (0600); value not displayed.")
