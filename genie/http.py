"""Use the OS trust store as well as requests' CA bundle; never disable TLS checks."""
import ssl
import requests
from requests.adapters import HTTPAdapter

class SystemTLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        kwargs['ssl_context'] = ssl.create_default_context()
        return super().init_poolmanager(*args, **kwargs)

def http_session():
    session = requests.Session()
    session.mount('https://', SystemTLSAdapter())
    return session
