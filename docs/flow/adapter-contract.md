# Contratto degli adapter

Gli adapter traducono in entrambe le direzioni: richiesta neutra → formato del provider, e risposta/errore del provider → formato normalizzato. Il resto dell'app conosce solo i tipi definiti qui.

## Tabella modelli (hardcoded)

Modelli e prezzi sono costanti nel codice, in un'unica tabella. Per ogni voce:

| Campo | Significato |
|---|---|
| `provider` | es. `anthropic`, `openai`, `oci` |
| `model_id` | id del modello presso il provider |
| `label` | nome mostrato in ASK_CHOICE |
| `price_input` | prezzo per milione di token in input |
| `price_output` | prezzo per milione di token in output |
| `currency` | valuta dei prezzi: `USD` per tutti i modelli |
| `max_output_tokens` | sempre valorizzato (alcuni provider lo richiedono obbligatoriamente) |
| `context_window` | token totali (prompt + risposta) accettati dal modello; serve per I17 |
| `long_context_threshold` | token di input oltre i quali il provider applica un prezzo maggiorato; `None` se non esiste |

Un solo modello per servizio:

| `provider` | `model_id` | `price_input` / `price_output` (USD per 1M token) | `context_window` | `long_context_threshold` |
|---|---|---|---|---|
| `anthropic` | `claude-haiku-5-5` | 0,10 / 0,50 | 1M | 100K |
| `openai` | `gpt-6-luna` | 0,10 / 0,50 | 1.050.000 | 272K |
| `oci` | `openai.gpt-oss-120b` | 0,15 / 0,60 | 128K | nessuna |

## Richiesta neutra

```python
@dataclass(frozen=True)
class LLMRequest:
    run_id: str
    provider: str
    model_id: str
    system_prompt: str        # template compilato + istruzione lingua (I7) + regola sui delimitatori (I8)
    transcript: str           # inserito tra delimitatori dall'adapter o da BUILD_PROMPT, mai concatenato nudo
    template_id: str
    template_language: str    # it | en | es
    response_language: str    # it | en | es | auto
    max_output_tokens: int    # dalla tabella modelli
```

## Risposta normalizzata

```python
@dataclass(frozen=True)
class Usage:
    input_tokens: int | None
    output_tokens: int | None

@dataclass(frozen=True)
class Cost:
    amount: Decimal | None    # None se non calcolabile, mai 0 come ripiego
    currency: str

@dataclass(frozen=True)
class LLMResponse:
    text: str
    status: Literal["complete", "truncated"]
    usage: Usage
    cost: Cost
    latency_ms: int
```

## Errori normalizzati

Ogni eccezione che esce da un adapter è una di queste. La classificazione si basa su **status HTTP e corpo della risposta**, non solo sullo status.

| Classe | Casi tipici | `retryable` |
|---|---|---|
| `TimeoutLLMError` | timeout di connessione o lettura; connessione assente, DNS non risolto, connessione rifiutata | sì |
| `RateLimitError` | 429 per limite di frequenza | sì |
| `ProviderError` | 5xx, provider sovraccarico | sì |
| `InsufficientCreditsError` | credito esaurito (vedi nota) | no |
| `AuthError` | 401/403, key revocata | no |
| `ModelNotFoundError` | modello ritirato o inesistente | no |
| `BadRequestError` | 400 per input non valido | no |
| `ContentFilteredError` | risposta bloccata dal provider | no |
| `InvalidResponseError` | risposta vuota o malformata | no |
| `UnknownLLMError` | qualsiasi altra eccezione (catch-all) | no |

Ogni errore porta anche `usage` e `cost` se il provider li ha restituiti, e un messaggio per l'utente senza dettagli tecnici né key.

**Nota credito esaurito**: OpenAI lo segnala con HTTP 429 e codice `insufficient_quota` nel corpo, cioè lo stesso status del rate limit. Anthropic lo segnala con un 400 e un messaggio sul saldo crediti. Se l'adapter guarda solo lo status, il credito esaurito viene classificato come ritentabile: è un errore. Per ogni provider va verificato come segnala questo caso.

## Interfaccia

```python
class ProviderAdapter(Protocol):
    provider: str
    def is_configured(self) -> bool: ...                 # solo lettura Portachiavi/config, nessun import SDK, nessuna rete
    def complete(self, req: LLMRequest) -> LLMResponse: ...  # solleva solo LLMError
```

`complete` esegue, in ordine: traduzione della richiesta → chiamata → traduzione della risposta (incluso `finish_reason` → `complete`/`truncated`, o `ContentFilteredError`) → calcolo costo.

## Configurazione dei client SDK
- `max_retries=0` su ogni client: gli SDK OpenAI e Anthropic ritentano automaticamente per default, il che violerebbe I10.
- Timeout espliciti da costanti (vedi parametri in `flow.md`).
- Import dell'SDK dentro l'adapter, al primo uso, non a livello di modulo.

## Costi

Funzione pura condivisa, non duplicata negli adapter:

```
costo = input_tokens × price_input / 1e6 + output_tokens × price_output / 1e6
```
Token mancanti o modello assente dalla tabella → `amount = None`.

**Stima mostrata in ASK_CHOICE** (prima della chiamata):
```
X = token stimati del transcript (caratteri / 3) + token del template
stima = X × price_input / 1e6 + min(X, max_output_tokens) × price_output / 1e6
```
Il riassunto si assume non più lungo del transcript, quindi la stima è un limite superiore ragionevole. Mostrata nella valuta della tabella, per esempio "max ~$0.006".

**Contesto sufficiente** (I17), con la stessa `X`:
```
X ≤ MAX_INPUT_TOKENS            (90.000, costante unica dell'app)
X + max_output_tokens ≤ context_window
```
`MAX_INPUT_TOKENS` deve restare sotto la soglia oltre la quale un provider applica prezzi long context: la tabella ha un solo prezzo per modello. Il controllo all'avvio verifica che per ogni modello `MAX_INPUT_TOKENS` sia ≤ della soglia long context dichiarata in tabella (`long_context_threshold`, assente se il provider non ne ha una).

## Vincoli
- Nessuna API key in log, errori, risultati o messaggi all'utente.
- Le API key si leggono dal Portachiavi di macOS (una voce per provider) solo dentro l'adapter, al momento della chiamata; `is_configured` verifica solo che la voce esista.
- Gli adapter sono sostituibili da fake nei test con la stessa interfaccia.
