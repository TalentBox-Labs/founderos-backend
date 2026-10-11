"""Create local Compose credentials without overwriting an existing .env."""
from pathlib import Path
import os
import secrets


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    destination = root / ".env"
    values = {name: secrets.token_urlsafe(32) for name in (
        "POSTGRES_PASSWORD", "SECRET_KEY", "RUNNER_API_KEY"
    )}
    # O_EXCL preserves existing configuration; 0600 protects new credentials.
    try:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print("Existing .env preserved. Ensure POSTGRES_PASSWORD, SECRET_KEY and RUNNER_API_KEY are set.")
        return 0
    with os.fdopen(fd, "w") as handle:
        handle.write("# Generated local development credentials; never use for production.\n")
        for name, value in values.items():
            handle.write(f"{name}={value}\n")
    print("Created local .env with private file permissions. Credentials were not printed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
