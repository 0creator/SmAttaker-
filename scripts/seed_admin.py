"""
SmAttaker — Seed Script: Create Admin User
"""
import asyncio
import os
import sys

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.database import async_session_factory, init_db
from backend.models.user import User, UserRole, UserStatus
from backend.config import settings


async def seed_admin():
    """Create the admin user if not exists."""
    await init_db()

    async with async_session_factory() as db:
        from sqlalchemy import select

        # Check if admin exists
        result = await db.execute(
            select(User).where(User.email == settings.ADMIN_EMAIL)
        )
        existing = result.scalar_one_or_none()

        if existing:
            print(f"✅ Admin already exists: {existing.email}")
            # Ensure admin role
            if existing.role != UserRole.ADMIN:
                existing.role = UserRole.ADMIN
                existing.status = UserStatus.ACTIVE
                existing.approved_by_admin = True
                await db.commit()
                print("  ✅ Updated to admin role")
            return

        # Create admin user
        admin = User(
            telegram_id=0,  # Update with real telegram ID
            telegram_username="SmAttakerAdmin",
            email=settings.ADMIN_EMAIL,
            full_name="SmAttaker Admin",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            approved_by_admin=True,
            language="en",
        )
        db.add(admin)
        await db.commit()

        print(f"✅ Admin created: {settings.ADMIN_EMAIL}")
        print(f"  Telegram ID: 0 (UPDATE THIS with real ID!)")
        print(f"  Role: admin | Status: active")


if __name__ == "__main__":
    asyncio.run(seed_admin())
