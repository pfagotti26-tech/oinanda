import os, sys, asyncio, pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault('MONGO_URL', 'mock')
import tempfile
os.environ.setdefault('MEDIA_DIR', tempfile.mkdtemp(prefix='oimedia'))

@pytest.fixture(autouse=True)
def banco_limpo():
    """Cada teste começa com o banco vazio (os arquivos de teste compartilham o mesmo banco em memória)."""
    from core import db
    async def limpa():
        for n in await db.list_collection_names(): await db[n].delete_many({})
    asyncio.run(limpa())
    yield
