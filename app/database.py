"""
Asynchronous database models and helper functions for the Valera bot.

We use SQLAlchemy's async support with SQLite by default.  If you set the
`DATABASE_URL` environment variable to a PostgreSQL URL, the bot will use it
instead.  SQLite is convenient for testing but not suitable for production
deployments on Heroku because the filesystem is read‑only and ephemereal.

To switch to Postgres in Heroku, add the `DATABASE_URL` config var and the
Heroku Postgres add‑on; SQLAlchemy will pick it up automatically.
"""
from __future__ import annotations

import datetime as dt
import os
from typing import Optional, Sequence

from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, Integer, String, select, func
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import declarative_base, relationship


# Derive the database URL from the environment.  If DATABASE_URL is not set,
# fall back to a SQLite database under the /tmp directory.  Heroku's file
# system is read‑only except for /tmp, so using /tmp allows the bot to
# persist data for the lifetime of a dyno.  Note that SQLite data stored in
# /tmp will be lost when the dyno restarts; for production use a Postgres
# DATABASE_URL instead.
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite+aiosqlite:///tmp/valera.db",
)

engine = create_async_engine(DATABASE_URL, echo=False, future=True)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)

Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False)
    username = Column(String(100), nullable=True)
    first_name = Column(String(100), nullable=True)
    last_name = Column(String(100), nullable=True)
    credits = Column(Integer, default=0, nullable=False)
    referred_by = Column(BigInteger, nullable=True)
    referral_bonus_granted = Column(Boolean, default=False, nullable=False)
    is_member = Column(Boolean, default=False, nullable=False)
    referral_code = Column(String(32), unique=True, nullable=True)
    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)
    pro_until = Column(DateTime(timezone=True), nullable=True)

    sessions = relationship("Session", back_populates="user")


class Session(Base):
    __tablename__ = "sessions"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    kind = Column(String(10), nullable=False)  # 'chat' or 'profile'
    input_meta = Column(String, nullable=True)
    input_text = Column(String, nullable=True)
    output_json = Column(String, nullable=True)
    tokens = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)

    user = relationship("User", back_populates="sessions")


async def init_db() -> None:
    """Create database tables if they do not exist."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_user(session: AsyncSession, telegram_id: int) -> Optional[User]:
    stmt = select(User).where(User.telegram_id == telegram_id)
    res = await session.execute(stmt)
    return res.scalar_one_or_none()


async def create_user(
    session: AsyncSession,
    telegram_id: int,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    referred_by: Optional[int] = None,
    referral_code: Optional[str] = None,
    initial_credits: int = 0,
) -> User:
    user = User(
        telegram_id=telegram_id,
        username=username,
        first_name=first_name,
        last_name=last_name,
        referred_by=referred_by,
        credits=initial_credits,
        referral_code=referral_code,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def add_credits(session: AsyncSession, user: User, amount: int, reason: str = "") -> None:
    user.credits += amount
    await session.commit()


async def deduct_credits(session: AsyncSession, user: User, amount: int) -> None:
    if user.credits < amount:
        raise ValueError("Insufficient credits")
    user.credits -= amount
    await session.commit()


async def set_membership(session: AsyncSession, user: User, is_member: bool) -> None:
    user.is_member = is_member
    await session.commit()


async def grant_referral_bonus(
    session: AsyncSession, user: User, bonus: int, referral_bonus: int
) -> None:
    """Grant referral bonuses to the referrer and the referred user if not already granted."""
    # user.referred_by holds the telegram_id of the referrer
    if user.referral_bonus_granted:
        return
    # Grant bonus to the new user
    user.credits += referral_bonus
    # Grant bonus to the referrer if exists
    if user.referred_by:
        stmt = select(User).where(User.telegram_id == user.referred_by)
        res = await session.execute(stmt)
        referrer = res.scalar_one_or_none()
        if referrer:
            referrer.credits += referral_bonus
    user.referral_bonus_granted = True
    await session.commit()
