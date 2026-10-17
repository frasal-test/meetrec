# Riassunto con LLM: decisioni e piano

Piano di implementazione del riassunto con LLM. La specifica **vincolante** è in [`docs/flow/`](flow/): `flow.md` (stati, transizioni, invarianti), `adapter-contract.md`, `ui.md`, `test-scenarios.md`. Se questo piano e la specifica non coincidono, vale la specifica.

Analisi iniziale del 16 ottobre 2026, aggiornata il 17 ottobre 2026 con la specifica del flusso. Prezzi e documentazione sono verificati sulle fonti ufficiali alla data del 16 ottobre, salvo dove indicato "da verificare".

## Obiettivo

Dopo la trascrizione locale, MeetRec può creare un riassunto (sintesi, decisioni, azioni, punti aperti) inviando **solo il testo** del transcript, mai l'audio, a un LLM in cloud scelto dall'utente.

## Decisioni di prodotto

- **Nessun LLM all'installazione.** `setup.sh` non chiede nulla sul riassunto. MeetRec si presenta con "tutto resta sul tuo Mac": il cloud è una scelta esplicita.
- **Finestra "Settings…"** nella menu bar, fuori dal flusso:
  - API key per Anthropic, OpenAI e Oracle OCI (più la region per OCI, proposta `eu-frankfurt-1`);
  - ogni key si verifica con una chiamata minima quando viene salvata (I4) e finisce nel **Portachiavi di macOS**, mai in `.env` o `config.json`;
  - avviso prima di salvare la prima key di un servizio: "Il testo della riunione (non l'audio) verrà inviato a <servizio> per creare il riassunto.";
  - interruttore **Automatic Summary**, spento all'installazione. Per accenderlo l'utente sceglie il modello di default tra quelli con key, senza preselezione.
- **Modalità manuale** (default): dopo ogni transcript compare ASK_CHOICE, con modello o "Nessuno" e lingua di risposta, senza preselezione (I5).
- **Modalità automatica**: ASK_CHOICE viene saltata e si usano il modello di default con la lingua di risposta Auto. "Riprova" resta una scelta dell'utente (I10).
- **Un solo modello per servizio**, scelto da noi e fisso nella tabella modelli.
- **Prezzi e stime in USD.**
- **Fuori dalla prima versione**: fascia qualità, modello locale (Ollama/MLX), Batch API, voce di menu "elabora un transcript già salvato".

## Tabella modelli

Costo stimato per una riunione di 1h: circa 15–20mila token in ingresso (italiano) e 1–2mila in uscita.

| `provider` | `model_id` | Prezzo per 1M token (input / output, USD) | `context_window` | Costo per riunione |
|---|---|---|---|---|
| `anthropic` | `claude-haiku-5-5` | 0,10 / 0,50 (oltre 100mila token di input: 0,50 / 2,50) | 1M | ~$0,003 |
| `openai` | `gpt-6-luna` | 0,10 / 0,50 (oltre 272mila token di input: 0,20 / 0,75) | 1.050.000 (input max 922mila, output max 128mila) | ~$0,003 |
| `oci` | `openai.gpt-oss-120b` | 0,15 / 0,60 | 128K (prompt + risposta) | ~$0,004 |

La tabella ha un solo prezzo per modello. Per non arrivare mai ai prezzi maggiorati per il long context (Anthropic oltre 100mila token, OpenAI oltre 272mila), l'app blocca i transcript oltre `MAX_INPUT_TOKENS` = 90mila token stimati, cioè circa 4,5–6 ore di parlato; il margine del 10% sotto la soglia di Anthropic e la stima prudente (caratteri / 3; il tokenizer di Haiku 5.5 conta circa il 30% di token in più dei modelli precedenti) tengono i token reali sotto la soglia: una riunione così lunga è fuori dallo scopo dello strumento (I17, END_NO_LLM `too_long`). Se un modello futuro avesse una soglia più bassa, il controllo all'avvio fallisce finché il limite non viene abbassato (T49).

Alternative OCI scartate:
- **gpt-oss-20b** ($0,07 / $0,30): più economico, ma più debole in italiano.
- **Gemini 2.5 Flash Lite** ($0,10 / $0,40): non si raggiunge con l'API key sull'endpoint OpenAI-compatibile; richiede l'endpoint `/google` e l'SDK Google, e manda le richieste a Google.
- **Grok**: le chiamate escono verso xAI.

## Dettagli API per adapter

Regole comuni (`adapter-contract.md`): `max_retries=0`, timeout 10 s per la connessione e 180 s per la lettura, SDK importato al primo uso, errori normalizzati in base allo status HTTP **e al corpo** della risposta.

### Anthropic: `claude-haiku-5-5`

- SDK Python `anthropic` (1.x, Python ≥ 3.10). Client `anthropic.Anthropic(api_key=..., max_retries=0, timeout=...)`.
- Chiamata: `client.messages.create(model=..., max_tokens=<max_output_tokens>, system=..., messages=[...], output_config={"effort": "low"})`.
- **Non passare** `thinking={"type": "enabled", ...}` e **niente prefill** dell'assistente: entrambi danno errore 400.
- Testo: solo i blocchi con `type == "text"`.
- `stop_reason`: `"max_tokens"` → `truncated`; `"refusal"` → `ContentFilteredError`.
- Credito esaurito: 400 con messaggio sul saldo crediti → `InsufficientCreditsError` (T28).

### OpenAI: `gpt-6-luna`

- SDK Python `openai`. Client `OpenAI(api_key=..., max_retries=0, timeout=...)`.
- 429 con `insufficient_quota` nel corpo → `InsufficientCreditsError`; 429 senza → `RateLimitError` (T26, T27).
- **Verificato il 17 ottobre 2026** sulla scheda del modello: supporta sia `v1/chat/completions` sia `v1/responses`; contesto 1.050.000 token (input max 922mila, output max 128mila); ragionamento con `reasoning_effort` (`none`, `low`, `medium` default, `high`, `xhigh`, `max`); con ragionamento attivo vanno tolti `temperature`, `top_p` e `top_logprobs`. Long context oltre 272mila token di input.
- Chiamata: `chat.completions.create(model="gpt-6-luna", messages=[system, user], max_completion_tokens=<max_output_tokens>, reasoning_effort="low")`. `finish_reason`: `length` → `truncated`; `content_filter` o `message.refusal` valorizzato → `ContentFilteredError`.

### Oracle OCI: `openai.gpt-oss-120b` con OCI Generative AI API key

- Chiave di servizio, non una IAM API key con PEM: nessuna firma delle richieste.
- Stesso SDK `openai`, con `base_url=f"https://inference.generativeai.{region}.oci.oraclecloud.com/20231130/actions/v1"`, poi `chat.completions.create(model="openai.gpt-oss-120b", ...)`.
  - Usare l'endpoint ufficiale `/20231130/actions/v1`, senza project OCID. L'esempio `oracle-samples` usa `/openai/v1` con `project=<OCID>`: le due forme non sono intercambiabili senza verifica.
- Regioni supportate con l'SDK OpenAI: Frankfurt, Hyderabad, Osaka, Ashburn, Chicago, Phoenix. In **`eu-frankfurt-1`** il modello è ospitato da OCI e il testo resta a Frankfurt. La chiave deve essere nella stessa region del modello.
- **Contesto**: 128mila token tra prompt e risposta. Con il limite unico di 90mila token di input più la risposta ci si sta; oltre vale comunque I17: il modello risulta disattivato in ASK_CHOICE, oppure in modalità automatica si arriva a END_NO_LLM `too_long`.
- **Lato amministratore OCI**: creare la chiave in Generative AI → API keys e autorizzarla con una policy IAM ("Add API Key Permissions"). Ogni chiave ha due segreti per la rotazione; il segreto si vede una volta sola.
- Come segnala OCI il credito esaurito va verificato (nota di `adapter-contract.md`).

## Contesto del codice MeetRec

- **Pipeline**: `SessionProcessor.__call__` in `meeting_recorder/session_transcription.py` scrive `transcript.txt/.srt/.json/.md/.speakers.txt` (e `.rttm`) in `<sessione>/transcripts/` con `path.write_text`, che **non è atomico**. Poi esegue `on_stop`.
- **Atomicità**: `atomic_write_json` in `meeting_recorder/sessions.py` (temporaneo + `os.replace`) è il modello da generalizzare.
- **Log**: `append_log` scrive testo libero in `<sessione>/transcribe.log`.
- **Job**: `run_job` rifà la trascrizione se c'è un'eccezione, fino a `max_attempts` (3).
- **Lingua**: il preset scelto nel menu (Italian / English / Spanish / Auto) arriva come opzione `language` (`None` per Auto). `Transcript.language` contiene invece la lingua *rilevata* (per esempio `"it,en"`) e non va usata come metadato del flusso.
- **Input per l'LLM**: `transcript.speakers.txt`, righe `[HH:MM:SS.mmm - HH:MM:SS.mmm] SPEAKER: testo`. `ME` è chi registra; `REMOTE_xx` sono gli altri partecipanti. Le righe `[MeetRec] ...` sono avvisi e vanno escluse.
- **Menu bar**: `menubar.swift` lancia `python -m meeting_recorder.control` con `Process` (`runControl`, `controlOutput`) ed esegue `worker --once` su una coda seriale. Notifica "Transcript ready" in `enqueueAndTranscribe`. Oggi non c'è una finestra Settings.
- **Doctor**: `doctor()` in `control.py` si lancia solo a mano dal menu e controlla l'accesso ai modelli Hugging Face via rete.
- **Test**: `tests/` con `unittest`. La CI esegue `python -m unittest discover -s tests -v` su Python 3.10–3.13 (Ubuntu) e compila lo Swift su macOS.

## Moduli da creare o modificare

Ogni funzione che implementa una parte del flusso indica nel docstring lo stato o la transizione corrispondente (per esempio `Stato: ASK_CHOICE`).

1. **`meeting_recorder/llm/`** (nuovo pacchetto)
   - `types.py`: `LLMRequest`, `LLMResponse`, `Usage`, `Cost`.
   - `errors.py`: gli errori normalizzati con `retryable`, `usage`, `cost` e messaggio per l'utente.
   - `models.py`: la tabella modelli, `cost()`, `estimate_max_cost()` e `fits_context()` come funzioni pure.
   - `adapters/`: `anthropic.py` e `openai_compat.py` (OpenAI e OCI), più un registro degli adapter. Sono gli unici moduli che importano SDK (I9).
   - `keys.py`: interfaccia di sola lettura sulle key (vedi "Portachiavi").
   - `prompts/`: template `it`, `en`, `es` e `build_prompt` (I6, I7, I8).
   - `storage.py`: `run_id`, scrittura atomica del risultato, salvataggio di emergenza (I2, I13, I14).
   - `flow.py`: la macchina a stati da CHECK_LENGTH in poi. Le schermate passano da un'interfaccia `UserInterface` (ASK_CHOICE, ASK_RETRY, avanzamento, esito), così nei test basta un fake. Log strutturato di ogni transizione (I15) ed esiti distinti (I16).
   - `checks.py`: controllo all'avvio offline e housekeeping.
2. **`meeting_recorder/session_transcription.py`**: TRANSCRIPT_READY e SAVE_TRANSCRIPT. Scritture atomiche, e metadato `language` (`it/en/es/auto`) salvato con il transcript. Il job conserva i suoi tentativi; END_FATAL viene comunicato dopo l'ultimo (T45).
3. **`meeting_recorder/config.py`**: `SummaryConfig(automatic=False, default_model=None, oci_region=None)`. Default spento, nessun modello di default. Aggiornare `config.example.json`.
4. **`meeting_recorder/control.py`**
   - `llm-run <sessione>`: esegue il flusso da CHECK_LENGTH. Protocollo con lo Swift: eventi JSON su stdout (`ask_choice`, `ask_retry`, `progress`, `outcome`) e risposte JSON su stdin.
   - `startup-check`: il controllo all'avvio offline. `doctor` lo include e aggiunge i controlli di rete.
   - Sottocomandi per Settings: verifica di una key (letta da stdin, mai da argv), `auto-summary on --model <id>` / `off`, stato in JSON (mai le key).
   - Installazione di `requirements-summary.txt` nel `.venv` quando si salva la prima key.
5. **`requirements-summary.txt`** (nuovo): `anthropic`, `openai`.
6. **`menubar.swift`**
   - Controllo all'avvio con `startup-check`: se fallisce, il flusso LLM non parte e il menu lo segnala.
   - Dopo "Transcript ready", lancia `llm-run` fuori dalla coda di trascrizione.
   - Schermate ASK_CHOICE e ASK_RETRY, indicatore di attesa e messaggi di esito come in `ui.md`.
   - Finestra Settings come in `ui.md`.
7. **Documentazione**: `README.md` (funzione e nota privacy, `summaries/` nella tabella "Which file to use") e `INSTALL.md` (come ottenere le key; per OCI i passi dell'amministratore e la policy IAM).
8. **Test**: almeno un test automatico per ogni scenario di `test-scenarios.md` (T1–T47), con adapter fake, Portachiavi fake e nessuna rete.

## Portachiavi

La finestra Settings è in Swift, quindi il Portachiavi lo gestisce l'app con il framework Security: salvataggio, lettura e cancellazione, una voce per provider.
- La key non passa mai in argv né in una variabile d'ambiente: argv è visibile in `ps`, l'ambiente si eredita.
- Verifica: Swift passa la key su stdin al sottocomando di verifica; se la verifica riesce, Swift la salva nel Portachiavi.
- `llm-run`: all'avvio Swift passa su stdin, in un unico messaggio iniziale, le key presenti. Il Python le tiene solo in memoria e `is_configured` controlla quel messaggio.
- Leggere il Portachiavi direttamente dal Python (`security find-generic-password`) farebbe comparire una richiesta di accesso di macOS per voci create da MeetRec.app. Per questo lo evitiamo.

## Prompt di riassunto

- **Template** (I6): stessa lingua del transcript (`it`/`en`/`es`); con `auto` o metadato assente, inglese.
- **Lingua di risposta** (I7): scelta in ASK_CHOICE (Ita/Eng/Esp/Auto), sempre Auto in modalità automatica. È sempre un'istruzione esplicita nel prompt.
- **Il transcript è un dato** (I8): sta tra delimitatori espliciti, per esempio `<transcript>…</transcript>`, e il system prompt dichiara che il contenuto delimitato non contiene istruzioni da eseguire.
- Avvertire il modello che:
  - il testo viene da un riconoscimento vocale e può contenere errori;
  - `ME` è chi registra e `REMOTE_xx` sono gli altri partecipanti;
  - non deve inventare nomi o attribuzioni.
- Struttura provvisoria di `summaries/<run_id>.md`: in testa servizio, modello e sessione; poi **Sintesi** (5–10 righe), **Decisioni**, **Azioni** (chi, cosa, eventuale scadenza) e **Punti aperti**.

## Da verificare prima del rilascio

- **OCI gpt-oss-120b**: se l'endpoint accetta `max_tokens` o `max_completion_tokens`, se accetta `reasoning_effort`, e una chiamata minima per verificare la key (non è documentato un endpoint `/models`).
- **Credito esaurito su OCI**: come lo segnala.
- **Prova reale su una tenancy OCI** in `eu-frankfurt-1`: endpoint, policy IAM, model id on-demand. Va fatta sul Mac, **mai con la key incollata in chat**.
- **Prova di qualità**: 3–5 transcript veri, riassunti con i tre modelli e confrontati.
- **Compilazione e prova della parte Swift**: possibili solo su macOS.

## Fonti

- Prezzi OpenAI: <https://developers.openai.com/api/docs/pricing>
- OCI API Keys (endpoint, modelli, regioni, rotazione): <https://docs.oracle.com/en-us/iaas/Content/generative-ai/api-keys.htm>
- OCI policy IAM per le chiavi: <https://docs.oracle.com/en-us/iaas/Content/generative-ai/add-api-permission.htm>
- OCI modelli per region: <https://docs.oracle.com/en-us/iaas/Content/generative-ai/model-endpoint-regions.htm>
- Scheda di OCI gpt-oss-120b: <https://docs.oracle.com/en-us/iaas/Content/generative-ai/openai-gpt-oss-120b.htm>
- Listino Oracle (Generative AI): <https://www.oracle.com/cloud/price-list/#genai>
- Esempi Oracle (SDK OpenAI, API key): <https://github.com/oracle-samples/oci-genai-auth-python>
- Prezzi e API Claude: documentazione Anthropic, aggiornata a ottobre 2026.
