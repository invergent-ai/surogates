"""Bootstrap a dev org + user for local testing.

Usage:
    SUROGATES_CONFIG=config.dev.yaml uv run python scripts/bootstrap_dev.py

Creates:
    - Org "dev"
    - User "admin@dev.local" with password "admin"
    - Prints the JWT token for immediate use
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import text

from surogates.config import load_settings
from surogates.db.engine import async_engine_from_settings, async_session_factory
from surogates.tenant.auth.database import DatabaseAuthProvider
from surogates.tenant.auth.jwt import create_access_token


async def main() -> None:
    settings = load_settings()
    engine = async_engine_from_settings(settings.db)
    factory = async_session_factory(engine)

    # Create all tables if they don't exist.
    from surogates.db.models import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("Database schema ensured.")

    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    email = "admin@dev.local"
    password = "admin"
    password_hash = DatabaseAuthProvider.hash_password(password)

    async with factory() as session:
        # Check if org already exists
        result = await session.execute(
            text("SELECT id FROM orgs WHERE name = :name"),
            {"name": "dev"},
        )
        existing = result.scalar_one_or_none()
        if existing:
            org_id = existing
            print(f"Org 'dev' already exists: {org_id}")
        else:
            await session.execute(
                text(
                    "INSERT INTO orgs (id, name, config) "
                    "VALUES (:id, :name, '{}'::jsonb)"
                ),
                {"id": org_id, "name": "dev"},
            )
            print(f"Created org 'dev': {org_id}")

        # Check if user already exists
        result = await session.execute(
            text("SELECT id FROM users WHERE email = :email AND org_id = :org_id"),
            {"email": email, "org_id": org_id},
        )
        existing_user = result.scalar_one_or_none()
        if existing_user:
            user_id = existing_user
            print(f"User '{email}' already exists: {user_id}")
        else:
            await session.execute(
                text(
                    "INSERT INTO users (id, org_id, email, display_name, auth_provider, password_hash) "
                    "VALUES (:id, :org_id, :email, :display_name, 'database', :password_hash)"
                ),
                {
                    "id": user_id,
                    "org_id": org_id,
                    "email": email,
                    "display_name": "Dev Admin",
                    "password_hash": password_hash,
                },
            )
            print(f"Created user '{email}': {user_id}")

        await session.commit()

    await engine.dispose()

    # Generate JWT
    token = create_access_token(org_id, user_id, {"admin"})

    print()
    print("=" * 60)
    print(f"  Email:    {email}")
    print(f"  Password: {password}")
    print(f"  Org ID:   {org_id}")
    print(f"  User ID:  {user_id}")
    print("=" * 60)
    print()
    print(f"JWT Token (admin):\n{token}")
    print()


if __name__ == "__main__":
    asyncio.run(main())
