# Riassunto automatico con LLM: decisioni e piano

Riepilogo della sessione di analisi del 16 ottobre 2026, da usare come punto di partenza per implementare la funzione. Prezzi e documentazione sono verificati sulle fonti ufficiali alla stessa data, salvo dove indicato "da verificare".

## Obiettivo

Dopo la trascrizione locale, MeetRec può generare `transcripts/summary.md` con sintesi, decisioni, azioni e punti aperti, inviando **solo il testo** del transcript (mai l'audio) a un LLM in cloud scelto dall'utente.

## Decisioni di prodotto (già prese)

Ragionate dal punto di vista dell'utente: niente scelte tecniche esposte.

- **Spento di default.** MeetRec si presenta con "tutto resta sul tuo Mac": il cloud è una scelta esplicita.
- **Niente domande sul riassunto in `setup.sh`.** MeetRec si installa senza LLM esterno; l'opzione si attiva dopo, dalla voce **"Settings…"** della menu bar.
- Nella finestra Settings c'è un interruttore "Automatic summary" (testo definitivo da decidere). Attivandolo compare la scelta del servizio:
  - menu a tendina (o radio button) "Anthropic / OpenAI / Oracle OCI";
  - il servizio scelto mostra solo i campi che servono: API key per tutti, più la region per OCI (proposta `eu-frankfurt-1`);
  - la chiave si verifica subito con una chiamata minima, come fa `check_token` con Hugging Face.
- **Modello fisso per servizio**, scelto da noi. Si può cambiare a mano in `config.json` insieme al base URL, ma non compare in Settings.
- **Chiavi in `.env`** (già in `.gitignore`, permessi 600), come `HF_TOKEN`. Mai in `config.json`.
- **Avviso prima di attivare**, nella finestra Settings: "Il testo della riunione (non l'audio) verrà inviato a <servizio> per creare il riassunto."
- **Fuori dalla prima versione**: scelta della fascia qualità nell'interfaccia, modello locale (Ollama/MLX), Batch API.


## Modelli scelti

Costo stimato per una riunione di 1h: circa 15–20mila token in ingresso (italiano) e 1–2mila in uscita.

| Servizio | Modello (id esatto) | Prezzo per 1M token (input / output) | Costo per riunione | Variabile in `.env` |
|---|---|---|---|---|
| Anthropic | `claude-haiku-5-5` | $0,10 / $0,50 (oltre 100mila token di input: $0,50 / $2,50) | ~$0,003 | `ANTHROPIC_API_KEY` |
| OpenAI | `gpt-6-luna` | $0,10 / $0,50 (cache $0,01; long context $0,20 / $0,75; Batch e Flex a metà prezzo) | ~$0,003 | `OPENAI_API_KEY` |
| Oracle OCI | `openai.gpt-oss-120b` | $0,15 / $0,60 | ~$0,004 | `OCI_GENAI_API_KEY` |

Con 20 riunioni al mese la spesa è di pochi centesimi: il prezzo non è un criterio di scelta.

Fascia qualità, per chi la imposta a mano in `config.json`:
- `claude-sonnet-5-5`: $2 / $10, circa $0,06 a riunione.
- `gpt-6.1-sol`: $2 / $10, circa $0,06 a riunione.
- Su OCI non c'è un equivalente recente. Gemini 2.5 Pro costa $1,25 / $10, ma non è disponibile con API key OpenAI-compatibile e fa chiamate esterne verso Google.

Alternative OCI scartate:
- **gpt-oss-20b** ($0,07 / $0,30): più economico, ma più debole in italiano.
- **Gemini 2.5 Flash Lite** ($0,10 / $0,40): non è raggiungibile con l'API key sull'endpoint OpenAI-compatibile, richiede l'endpoint `/google` e l'SDK Google, e manda le richieste a Google.
- **Grok**: le chiamate escono verso xAI.

## Dettagli API per servizio

### Anthropic: `claude-haiku-5-5`

- SDK Python `anthropic` (1.x, basato su `httpx2`, Python ≥ 3.10). Client `anthropic.Anthropic()`, che legge `ANTHROPIC_API_KEY`.
- Chiamata: `client.messages.create(model="claude-haiku-5-5", max_tokens=16000, system=..., messages=[...], output_config={"effort": "low"})`.
- **Effort `low`**: il ragionamento è sempre attivo e si paga come output; il default è `medium`. Per un riassunto basta `low`.
- **Non passare** `thinking={"type": "enabled", "budget_tokens": ...}` (errore 400). Non serve passare `thinking`.
- **Niente prefill** dell'assistente (errore 400).
- Nella risposta, leggere solo i blocchi con `type == "text"`. Possono esserci blocchi `thinking` vuoti.
- Controllare `stop_reason`:
  - `"max_tokens"` significa riassunto troncato;
  - `"refusal"` significa rifiuto, e su Haiku 5.5 non c'è un fallback lato server: registrarlo come avviso.
- Errori: catena `RateLimitError` → `APIStatusError` → `APIConnectionError`. L'SDK fa già 2 tentativi su 429 e 5xx.
- Contesto da 1M di token: qualsiasi riunione entra in una sola chiamata.

### OpenAI: `gpt-6-luna`

- SDK Python `openai`, client `OpenAI()`, che legge `OPENAI_API_KEY`.
- Chiamata: `client.chat.completions.create(model="gpt-6-luna", messages=[{"role": "system", ...}, {"role": "user", ...}])`.
- **Da verificare** sulla documentazione del modello:
  - che Luna supporti Chat Completions oppure solo Responses API;
  - il parametro per limitare il ragionamento;
  - la soglia short/long context, che non è sulla pagina prezzi.
- Il contesto dichiarato da fonti di terzi è di circa 1M di token.

### Oracle OCI: `openai.gpt-oss-120b` con OCI Generative AI API key

- Chiave di servizio, annunciata il 21 gennaio 2026. **Non** è una IAM API key con PEM e fingerprint: nessuna firma delle richieste e nessun wrapper OCI.
- Stesso SDK `openai`:
  ```python
  OpenAI(
      api_key=os.environ["OCI_GENAI_API_KEY"],
      base_url=f"https://inference.generativeai.{region}.oci.oraclecloud.com/20231130/actions/v1",
  )
  ```
  poi `client.chat.completions.create(model="openai.gpt-oss-120b", messages=[...])`.
- **Endpoint ufficiale**: `/20231130/actions/v1`, dalla pagina Oracle "API Keys". L'esempio ufficiale **non usa il project OCID**.
  - Il repository `oracle-samples/oci-genai-auth-python` usa invece `/openai/v1` con `project=<OCID>`.
  - Non sono intercambiabili senza verifica: usare `/20231130/actions/v1`.
- Modelli supportati con API key:
  - Chat Completions: Meta Llama, xAI Grok, OpenAI gpt-oss;
  - Responses API: Grok e gpt-oss.
- Regioni supportate con l'SDK OpenAI: Frankfurt, Hyderabad, Osaka, Ashburn, Chicago, Phoenix.
  - **`eu-frankfurt-1`**: gpt-oss-120b è disponibile on-demand e ospitato da OCI, senza chiamate esterne, quindi il testo resta a Frankfurt.
  - La chiave deve essere nella stessa region del modello.
- **Contesto**: 128mila token in totale tra prompt e risposta, cioè circa 5–6 ore di riunione. Oltre, saltare il riassunto con un avviso nel log. Non troncare in silenzio.
- **Lato amministratore OCI**:
  - creare la chiave in Generative AI → API keys;
  - autorizzarla con una policy IAM (pagina "Add API Key Permissions").
  - Ogni chiave ha due segreti (key-one e key-two) per la rotazione. Il segreto si vede una volta sola.
- Nell'esempio Oracle la variabile si chiama `OPENAI_API_KEY`. Noi usiamo `OCI_GENAI_API_KEY` per non scontrarci con una chiave OpenAI vera.
- La compatibilità OpenAI non garantisce tutte le funzioni. A noi serve solo una chiamata di testo, senza streaming, tool o output strutturato.

## Contesto del codice MeetRec

- **Pipeline**: `meeting_recorder/session_transcription.py`, metodo `SessionProcessor.__call__`.
  - Scrive gli output in `session_dir / "transcripts"`: `transcript.txt/.srt/.json/.md/.speakers.txt` e `.rttm`.
  - Poi esegue l'hook `on_stop` con fase di avanzamento `on_stop` a 0.98.
  - Il riassunto va **dopo la scrittura degli output e prima di `on_stop`**, con `update_job_progress(phase="summarizing", fraction≈0.96, ...)`.
  - Se fallisce, deve fare come `on_stop`: `append_log(...)` senza invalidare il transcript.
- **Input per l'LLM**: `transcripts/transcript.speakers.txt`, scritto da `write_speaker_txt` in `meeting_recorder/transcription.py`.
  - Formato delle righe: `[HH:MM:SS.mmm - HH:MM:SS.mmm] SPEAKER: testo`.
  - `ME` è chi registra; `REMOTE_01`, `REMOTE_02`… sono gli altri partecipanti (`REMOTE` se la diarizzazione è saltata).
  - Eventuali righe iniziali `[MeetRec] ...` sono avvisi.
- **Config**: `meeting_recorder/config.py`.
  - Le dataclass `AppConfig` e `TranscriptionConfig` vengono riempite da `_merge_transcription`.
  - Il file è `~/.config/meetrec/config.json`; l'esempio è `config.example.json`.
  - `set_config_value` scrive una chiave di primo livello.
- **Variabili d'ambiente**: `load_env_files()` in `transcription.py` carica `.env` del progetto e della cwd con `setdefault`.
- **Setup**: `setup.sh` è bilingue: `say "english" "italiano"`, più `warn` e `ask_secret`.
  - Il token Hugging Face segue `read_token` → `check_token` → `save_token`.
  - `save_token` scrive `HF_TOKEN` in `.env` con un file temporaneo, `chmod 600` e `mv`. Per le chiavi LLM serve la stessa logica, ma in Python (vedi `control.py`), perché la scrittura parte da Settings e non da `setup.sh`.
  - La config si scrive da Python inline, come in `set_speaker_separation`.
- **Dipendenze opzionali**: esiste già `requirements-diarization.txt`; serve lo stesso schema per `requirements-summary.txt` (`anthropic`, `openai`).
- **Menu bar**: `menubar.swift`, metodo `rebuildMenu()`, con etichette in inglese ("Open recordings", "Run doctor", …).
  - La notifica "Transcript ready: <sessione>" è intorno alla riga 597.
  - Oggi **non c'è una finestra Settings**: solo voci di menu e `NSAlert`. Va costruita da zero.
  - Lo Swift chiama Python con `Process` su `python -m meeting_recorder.control <argomenti>` (`controlOutput`, intorno alla riga 187).
- **CLI**: `meeting_recorder/control.py` (sottocomandi argparse, tra cui `doctor`).
- **Test**: `tests/` con `unittest`. La CI (`.github/workflows/ci.yml`) esegue `python -m unittest discover -s tests -v` su Python 3.10–3.13 (Ubuntu), compila la parte Swift su macOS e controlla gli script con `bash -n`.
  - I test non devono usare la rete: fare mock dei client.

## Moduli da creare o modificare

1. **`meeting_recorder/summary.py`** (nuovo)
   - `summarize_session(session_dir, config) -> Path | None`: legge `transcript.speakers.txt`, chiama il servizio e scrive `transcripts/summary.md`.
   - Un'interfaccia comune: `summarize(text, language) -> str`, con due implementazioni:
     - `AnthropicSummarizer`;
     - `OpenAICompatibleSummarizer`, che serve sia OpenAI sia OCI cambiando `base_url`, nome della variabile della chiave e modello.
   - Import pigri di `anthropic` e `openai`, così chi non attiva il riassunto non ha bisogno dei pacchetti.
   - Stima della dimensione prima della chiamata, con un limite per servizio (OCI 128mila token in totale).
   - Ritorna avvisi leggibili; non lancia eccezioni che blocchino la pipeline.
2. **`meeting_recorder/config.py`**
   - Nuova `SummaryConfig`: `enabled=False`, `provider: str | None` (`"anthropic" | "openai" | "oci"`), `model: str | None` (default per servizio), `region="eu-frankfurt-1"` (solo OCI), `base_url: str | None` (override).
   - `_merge_summary` con validazione del provider. Campo `summary` in `AppConfig`.
   - Aggiornare `config.example.json`.
3. **`meeting_recorder/session_transcription.py`**: chiamata a `summarize_session` dopo gli output e prima di `on_stop`, con fase di avanzamento e log.
4. **`meeting_recorder/control.py`**
   - Sottocomando `summarize <sessione>`, per rifare o provare il riassunto su una sessione già trascritta.
   - Sottocomandi usati dalla finestra Settings, così lo Swift non scrive mai `.env` o `config.json` da solo:
     - `summary-status`: stato attuale in JSON (attivo, servizio, region, chiave presente sì/no; mai la chiave);
     - `summary-enable --provider <p> [--region <r>]`: legge la chiave da **stdin** (mai come argomento, sarebbe visibile in `ps`), installa `requirements-summary.txt` nel `.venv` se mancano i pacchetti, verifica la chiave con una chiamata minima, la salva in `.env` (file temporaneo, `chmod 600`, `mv`) e scrive il blocco `summary` nella config. Se qualcosa fallisce non cambia nulla e restituisce un messaggio leggibile;
     - `summary-disable`: mette `enabled=false` e lascia la chiave in `.env`.
   - In `doctor`, se il riassunto è attivo: pacchetto installato, chiave presente e servizio valido.
5. **`setup.sh`**: nessuna modifica per il riassunto. L'installazione resta senza LLM esterno.
6. **`requirements-summary.txt`** (nuovo): `anthropic`, `openai`. Viene installato da `summary-enable`, non da `setup.sh`.
7. **`menubar.swift`**
   - Voce "Settings…" nel menu, che apre una piccola finestra (AppKit, come il resto dell'app):
     - interruttore "Automatic summary";
     - scelta del servizio, campo API key (`NSSecureTextField`) e, solo per OCI, la region;
     - l'avviso privacy prima di confermare;
     - pulsante di conferma che lancia `summary-enable` in background, con indicatore di attesa e messaggio di esito.
   - All'apertura legge lo stato con `summary-status`.
   - Voce "Open summary" quando la sessione ha `transcripts/summary.md`.
   - La notifica dice che il riassunto è pronto.
8. **Documentazione**
   - `README.md`: funzione e nota privacy; tabella "Which file to use" con `summary.md`.
   - `INSTALL.md`: come attivare il riassunto da Settings e come ottenere le chiavi; per OCI, i passi dell'amministratore e la policy IAM.
9. **Test**: config (default spento, provider non valido), scelta del modello di default, scrittura di `summary.md` con client finti, gestione di errori, rifiuti e testo troppo lungo, `summary-enable` / `summary-disable` / `summary-status` (chiave letta da stdin, `.env` con permessi 600, nessuna modifica se la verifica fallisce).

## Prompt di riassunto

- **Lingua del riassunto**: sempre quella della riunione.
  - Con `transcription.language` impostato (it / en / es) c'è un prompt scritto in quella lingua.
  - Con il rilevamento automatico il prompt è in inglese e chiede al modello di scrivere il riassunto nella lingua prevalente della riunione.
- **Il transcript è un dato, non un'istruzione.** Va nel messaggio utente dentro un delimitatore, per esempio `<transcript>…</transcript>`. Nel prompt di sistema va scritto di ignorare le istruzioni contenute nel parlato.
- Avvertire il modello che:
  - il testo viene da un riconoscimento vocale e può contenere errori;
  - `ME` è chi registra e `REMOTE_xx` sono gli altri partecipanti;
  - non deve inventare nomi o attribuzioni.
- Struttura provvisoria di `summary.md`:
  - **Sintesi**: 5–10 righe;
  - **Decisioni**;
  - **Azioni**: chi, cosa ed eventuale scadenza;
  - **Punti aperti**.
  - In testa una riga con servizio e modello usati e il nome della sessione.

## Da verificare prima del rilascio

- **Prova reale su una tenancy OCI** in `eu-frankfurt-1`: endpoint `/20231130/actions/v1`, policy IAM, model id `openai.gpt-oss-120b` on-demand.
  - Va fatta sul Mac o con la chiave come secret dell'ambiente, **mai incollata in chat**.
- **GPT-6 Luna**: Chat Completions o Responses API, parametro di ragionamento, soglia del long context.
- **Prova di qualità**: 3–5 transcript veri di MeetRec, riassunti con Haiku 5.5, Luna e gpt-oss-120b, poi confrontati. Costo totale: pochi centesimi.
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
