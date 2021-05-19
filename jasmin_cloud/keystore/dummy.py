from .base import KeyStore


class DummyKeyStore(KeyStore):
    """
    Key store implementation that returns a single key for all users.

    Args:
        key: The single public key to use for all users.
    """
    def __init__(self, key = None):
        self.key = key

    def get_key(self, username):
        return self.key
