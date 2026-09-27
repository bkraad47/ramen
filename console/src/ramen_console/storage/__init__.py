import os

from .base import Store
from .encrypted import EncryptedStore, FieldCipher


def make_store(firestore_client=None) -> Store:
    kind = os.environ.get("RAMEN_STORE", "memory")
    if kind == "memory":
        from .memory import MemoryStore
        store: Store = MemoryStore()
    elif kind == "firestore":
        from .firestore import FirestoreStore
        store = FirestoreStore(firestore_client) if firestore_client else FirestoreStore.from_env()
    elif kind == "dynamodb":
        from .dynamodb import DynamoStore
        store = DynamoStore.from_env()
    else:
        raise ValueError(f"unknown RAMEN_STORE={kind!r}; use memory|firestore|dynamodb")
    key = os.environ.get("RAMEN_FERNET_KEY")
    return EncryptedStore(store, FieldCipher(key)) if key else store
