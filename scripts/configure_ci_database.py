"""Export a masked DATABASE_URL from repository secrets for subsequent CI steps.

Run only in GitHub Actions after dependencies are installed. No DB connection
is opened, and credentials are never interpolated into shell command text.
"""

import os
from pathlib import Path

from sqlalchemy.engine import URL

REQUIRED = ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME")


def build_database_url(values) -> str:
    missing = [key for key in REQUIRED if not values.get(key)]
    if missing:
        raise ValueError("Missing repository secrets: " + ", ".join(missing))
    if any("\n" in values[key] or "\r" in values[key] for key in REQUIRED):
        raise ValueError("Database secrets must be single-line values")
    try:
        port = int(values["DB_PORT"])
    except ValueError:
        raise ValueError("DB_PORT must be an integer") from None
    if not 1 <= port <= 65535:
        raise ValueError("DB_PORT must be between 1 and 65535")
    return URL.create(
        "postgresql", host=values["DB_HOST"], port=port,
        username=values["DB_USER"], password=values["DB_PASSWORD"],
        database=values["DB_NAME"], query={"sslmode": "require"},
    ).render_as_string(hide_password=False)


def main() -> int:
    try:
        url = build_database_url(os.environ)
    except ValueError as error:
        # Validation errors contain variable names only, never their values.
        print(f"::error::{error}")
        return 1
    env_file = os.environ.get("GITHUB_ENV")
    if not env_file:
        print("This helper requires the GitHub Actions GITHUB_ENV file.")
        return 1
    # Mask the encoded credential as well as the complete derived URL.
    encoded_password = url.split("://", 1)[1].split("@", 1)[0].split(":", 1)[1]
    # Escape workflow-command syntax so literal percent sequences stay literal.
    for secret in (encoded_password, url):
        escaped = secret.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::add-mask::{escaped}")
    with Path(env_file).open("a", encoding="utf-8") as target:
        target.write(f"DATABASE_URL={url}\n")
    print("Database connection configured; TLS is required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
