from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from ..config import settings

engine = create_async_engine(
    settings.database_url,
    echo=settings.sql_echo,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_pre_ping=True,
    pool_recycle=3600,
)

AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Smaller pool for the worker process (2 + 1 overflow = max 3 connections).
worker_engine = create_async_engine(
    settings.database_url,
    echo=settings.sql_echo,
    pool_size=2,
    max_overflow=1,
    pool_pre_ping=True,
    pool_recycle=3600,
)

WorkerSessionLocal = sessionmaker(worker_engine, class_=AsyncSession, expire_on_commit=False)
