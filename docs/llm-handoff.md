# Riassunto LLM: stato del lavoro (17 ottobre 2026)

Punto di ripartenza per una nuova sessione. Leggere prima `CLAUDE.md` (regole vincolanti) e la specifica in `docs/flow/` (`flow.md`, `adapter-contract.md`, `ui.md`, `test-scenarios.md`); il piano tecnico è `docs/llm-summary-plan.md`.

## Dove si lavora
- **Beta**: `~/Projects/meetrec-beta`, git worktree sul branch `ccr-6635c104-1y950y`, con il suo `.venv` (dipendenze di trascrizione, diarizzazione e `requirements-summary.txt`) e il suo `MeetRec.app`. Si compila con `./meetrec.sh build` **solo lì** e solo con la beta chiusa (controllare con `pgrep -fl "meetrec-beta/MeetRec.app"`). Non è installata come LaunchAgent: l'utente la apre con `open ~/Projects/meetrec-beta/MeetRec.app` dopo aver chiuso l'ufficiale (una sola istanza alla volta).
- **Ufficiale**: `~/Projects/meetrec` resta su `main`. È ciò che scarica chi installa (`setup.sh` clona `main`, repository pubblico `frasal-test/meetrec`). Non va mai portata sul branch della beta, e il lavoro LLM non va unito a `main` senza una decisione esplicita di rilascio.
- Commit e push solo sul branch della beta. Lingua del codice e dei commit: inglese; documenti e messaggi all'utente: italiano.
- Codice scritto da Sonnet (sotto-agenti), orchestrazione e revisione da Opus; effort scelto caso per caso.

## Cosa è fatto (tutto committato e pushato)
- **Specifica** in `docs/flow/`, aggiornata con tutte le decisioni dell'utente.
- **Python** (`meeting_recorder/llm/`): tipi, errori normalizzati, tabella modelli, costi, prompt `it`/`en`/`es` (template v3), salvataggio atomico dei risultati in `<sessione>/summaries/<run_id>.json|.md`, salvataggio di emergenza in `~/Library/Application Support/MeetRec/emergency/`, log strutturato in `<sessione>/llm.log`, macchina a stati da CHECK_LENGTH in poi, controllo all'avvio offline con pulizia.
- **Adapter**: Anthropic (`claude-haiku-5-5`, effort low) e OpenAI-compatibile per OpenAI (`gpt-6-luna`, Chat Completions, `max_completion_tokens`, `reasoning_effort` low) e OCI (`openai.gpt-oss-120b`, `/20231130/actions/v1`, `max_tokens`). Nessun retry degli SDK, timeout 10/180 s, URL ufficiali fissati.
- **Collegamento**: il worker esegue SAVE_TRANSCRIPT (scritture atomiche, `language_setting` in `transcript.json`, `TranscriptSaveError`); `control.py` ha `llm-run` (protocollo JSON su stdin/stdout, codici di uscita 0/10/11/12/13/20/21/30), `startup-check`, `doctor` in due fasi, `llm-key-verify`, `auto-summary status|on|off`.
- **Swift** (`menubar.swift`, `LLMSummary.swift`): Portachiavi (servizio `com.frasal.meetrec.llm`, una voce per provider; le key arrivano al Python solo su stdin), ASK_CHOICE, ASK_RETRY, indicatore di attesa, notifica di esito, finestra Settings con avvertenza privacy sempre visibile, controllo all'avvio, notifiche native (UserNotifications) al posto di `osascript`.
- **Test**: 320 test Python, tutti verdi (`.venv/bin/python -m unittest discover -s tests`). Ogni scenario T1–T49 ha almeno un test, tranne T46 (la parte Swift di Settings, provata a mano).

## Decisioni dell'utente da ricordare
- Modo manuale di default; modo automatico ("Automatic Summary") con modello scelto in Settings senza preselezione e risposta Auto. "Riprova" sempre offerto per errori ritentabili, anche in automatico.
- Limite di input 90.000 token stimati (caratteri / 3), sotto ogni fascia di prezzo long context; oltre: `too_long`.
- Errori di connessione ritentabili come i timeout.
- Prezzi in USD; stima "max ~$" con tre decimali sotto $0.10.
- Sezione **Partecipanti**: nome o ruolo solo se detti nel transcript, ruolo dedotto come "probabilmente …", mai un nome dedotto.
- Titoli delle sezioni e intestazione del `.md` nella lingua di risposta.
- La beta può essere visibile su GitHub; ciò che si scarica deve restare `main`.
- Tutta la UI è in inglese, compresa la parte LLM e le fasi della trascrizione; il `.md` del riassunto resta nella lingua di risposta.

## Provato dall'utente sulla beta (17 ottobre)
Settings con una, due e tre key (Anthropic, OpenAI, OCI in `eu-frankfurt-1`), ASK_CHOICE con uno e due modelli, modo automatico, transcript troppo corto, riassunti con Anthropic e OCI (OCI verificato: endpoint, policy IAM, `max_tokens`, `usage`). Tutto corretto.

## Da fare
1. **Ricompilare la beta** quando l'utente la chiude: servono le notifiche native e l'avvertenza privacy in Settings (commit `b4ea6f6` e `7f2cdab`), che la beta aperta non ha ancora. Poi verificare con l'utente: permesso alle notifiche al primo avvio, clic sulla notifica che apre il `.md`.
2. **Test manuali ancora da fare** (lista data all'utente): Riprova senza rete, key sbagliata, "Nessuno" e chiusura della finestra, modo automatico più "Rimuovi", salvataggio di emergenza con `chmod a-w` sulla sessione, nessun LLM configurato, doctor dal menu.
3. **Script di prova reale** che l'utente lancia da sé con le proprie key (mai incollate in chat).
4. **README e INSTALL** per quando si deciderà il rilascio: funzione, nota privacy, `summaries/`, come ottenere le key (per OCI: Console → Generative AI → API keys in Frankfurt, policy IAM da "Add API Key Permissions").
5. **Aperti, da decidere con l'utente**:
   - la Sintesi esce a volte come elenco puntato invece di 5–10 righe;
   - voce "elabora un transcript già salvato" (fuori ambito, ma utile per confrontare i modelli);
   - come OCI segnala il credito esaurito (non verificabile finché non succede).

## Licenza (decisa il 17 ottobre)
GPL-3.0 da dopo v0.9.1 (commit `c000c7c` su `main`, portato sulla beta). v0.9 e v0.9.1 restano MIT. Nessun CLA per ora: servirebbe solo per una futura doppia licenza commerciale con contributi esterni.
