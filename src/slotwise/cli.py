"""Operational CLI. `python -m slotwise.cli create-superadmin --email ... --password ...`"""

import argparse
import asyncio

from slotwise.config import get_settings
from slotwise.db import Database
from slotwise.models import User
from slotwise.repositories.tenancy import UserRepository
from slotwise.security.passwords import hash_password


async def create_superadmin(email: str, password: str, name: str) -> None:
    settings = get_settings()
    db = Database(settings.database_url, settings, pooled=False)
    async with db.session() as session:
        users = UserRepository(session)
        user = await users.get_by_email(email)
        if user is None:
            users.add(
                User(
                    email=email,
                    password_hash=hash_password(password),
                    full_name=name,
                    is_superadmin=True,
                )
            )
        else:
            user.is_superadmin = True
        await session.commit()
    await db.dispose()
    print(f"superadmin ready: {email}")  # noqa: T201


def main() -> None:
    parser = argparse.ArgumentParser(prog="slotwise")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sa = sub.add_parser("create-superadmin")
    sa.add_argument("--email", required=True)
    sa.add_argument("--password", required=True)
    sa.add_argument("--name", default="Platform Admin")
    args = parser.parse_args()
    if args.cmd == "create-superadmin":
        asyncio.run(create_superadmin(args.email, args.password, args.name))


if __name__ == "__main__":
    main()
