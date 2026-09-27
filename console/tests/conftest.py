import os

os.environ.setdefault("RAMEN_GCP_FRESH_HTTP", "0")  # fakes never need a real transport; avoids ADC probing

import os

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("RAMEN_FERNET_KEY", Fernet.generate_key().decode())
os.environ.setdefault("RAMEN_SESSION_SECRET", "test-session-secret")


@pytest.fixture
def fernet_key():
    return os.environ["RAMEN_FERNET_KEY"]
