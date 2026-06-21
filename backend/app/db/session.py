from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

# Pool sized for digestion concurrency. digest_document now releases
# its session during CPU-bound work (extraction/chunking/embedding),
# but during the persist phase it holds one connection per batch.
# With pool_size=10 + max_overflow=20 we can handle ~30 concurrent
# DB-needing operations without starving other requests of connections.
# pool_pre_ping guards against stale connections after a DB restart.
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    pool_timeout=30,  # fail fast instead of hanging when pool is exhausted
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency that provides a database session."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
