"""Constants shared by the adapters (docs/flow/adapter-contract.md). No SDK is
imported here (I9)."""
from __future__ import annotations

# docs/flow/flow.md: HTTP timeouts, configurable constants.
CONNECT_TIMEOUT_SECONDS = 10.0
READ_TIMEOUT_SECONDS = 180.0

CONNECTION_MESSAGE = "Impossibile raggiungere il servizio: controlla la connessione."

# Explicit endpoints: without them the SDKs read OPENAI_BASE_URL and
# ANTHROPIC_BASE_URL from the environment, which could send the key and the
# transcript to another host.
ANTHROPIC_BASE_URL = "https://api.anthropic.com"
OPENAI_BASE_URL = "https://api.openai.com/v1"
