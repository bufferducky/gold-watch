import pytest
from cryptography.fernet import Fernet

from gold_watch.models import Quote, now_local
from gold_watch.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "gold-watch.db", Fernet.generate_key().decode())


@pytest.fixture
def quote():
    return Quote(kind="international", symbol="XAU/USD", name="国际现货黄金", price="3900.25",
                 currency="USD", unit="美元/金衡盎司", quote_time=now_local(), fetched_at=now_local(),
                 source="Test provider", source_url="https://example.com/", price_type="spot")
