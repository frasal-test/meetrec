# Interfaccia utente

L'app non ha terminale: tutte le interazioni sono menu e bottoni. Come il resto di MeetRec, la UI è in **inglese**: i testi qui sotto sono quelli mostrati (le note restano in italiano). Due sole schermate di scelta nel flusso: ASK_CHOICE e ASK_RETRY. La finestra Settings è fuori dal flusso.

## ASK_CHOICE — schermata unica

```
┌──────────────────────────────────────────┐
│ Transcript saved                         │
│ <sessione> · <lingua> · ~<N> words       │
│                                          │
│ Summarize with                           │
│ ( ) None                 transcript only │
│ ( ) <label modello 1>       max ~$0.006  │
│ ( ) <label modello 2>        max ~$0.12  │
│  -  <label modello 3>  transcript too    │
│                        long              │
│                                          │
│ Response language                        │
│ [Ita] [Eng] [Esp] [Auto]                 │
│                                          │
│              [ Start ]                   │
└──────────────────────────────────────────┘
```

- Nessuna opzione preselezionata (I5).
- Elenco modelli: solo quelli con key configurata, nell'ordine della tabella modelli.
- Accanto a ogni modello la stima di costo massimo in USD (vedi `adapter-contract.md`), arrotondata per eccesso: tre decimali sotto $0.10, due sopra.
- `<sessione>` è il nome della cartella della sessione, perché i file del transcript hanno lo stesso nome in ogni sessione.
- Un modello con key ma senza contesto sufficiente (I17) appare disattivato, con "transcript too long", e non è selezionabile.
- Scelto "None": la lingua si disattiva, il bottone diventa "Close".
- "Start" è attivo solo con modello + lingua scelti.
- Lingua del transcript `auto`: mostrata come "auto-detect".

In modalità automatica ASK_CHOICE non viene mostrata, salvo il caso in cui il modello di default non abbia più la key (vedi `flow.md`).

## Durante la chiamata
Indicatore di attesa con il nome del modello ("Summarizing with <modello>…"). Nessuna interazione richiesta.

## ASK_RETRY
Mostrata solo per errori ritentabili al primo tentativo.

```
<messaggio errore per l'utente>
[ Retry ]  [ Cancel ]
```
Nessun default.

## Messaggi di esito

| Esito | Messaggio |
|---|---|
| DONE `complete` | Summary saved to <percorso> · <modello> · <costo reale, o "cost unavailable"> |
| DONE `truncated` | Come sopra + ". The response was cut short by the length limit" |
| END_NO_LLM `too_short` | Transcript too short for a summary, saved to <percorso> |
| END_NO_LLM `no_keys` | Transcript saved. No LLM configured |
| END_NO_LLM `too_long` | Transcript too long for a summary (over ~5 hours) / for <modello>. Transcript saved |
| END_NO_LLM `declined` | Transcript saved |
| END_ERROR `InsufficientCreditsError` | No credit left on <provider>. Transcript saved |
| END_ERROR `AuthError` | Could not use the <provider> API key: check it in Settings. Transcript saved (key revocata, oppure non letta dal Portachiavi) |
| END_ERROR `ModelNotFoundError` | Model no longer available from <provider>: update the app. Transcript saved |
| END_ERROR `ContentFilteredError` | The provider blocked the response. Transcript saved |
| END_ERROR altri | Error while processing (<tipo>). Transcript saved |
| END_SAVE_ERROR | Response saved as an emergency copy in <percorso> |
| END_FATAL | Could not save the transcript (<motivo>) |

Nessun messaggio contiene dettagli tecnici grezzi o API key. Il riassunto salvato (`.md`), invece, ha titoli e intestazione nella lingua di risposta.

## Settings (fuori dal flusso)

```
┌──────────────────────────────────────────┐
│ API key                                  │
│ Anthropic   [ ••••••••  ] [Save] ✓       │
│ OpenAI      [           ] [Save]         │
│ Oracle OCI  [           ] Region [     ] │
│             [Save]                       │
│                                          │
│ [ ] Automatic Summary                    │
│     Model: ( ) <label 1>  ( ) <label 2>  │
└──────────────────────────────────────────┘
```

- Le key vanno nel Portachiavi di macOS. Ogni key è verificata con una chiamata minima quando viene salvata (I4); se la verifica fallisce non viene salvata.
- Sempre visibile sotto "API key": "With an LLM, the meeting text (never the audio) is sent to the service you choose: Anthropic, OpenAI or Oracle. From then on, your data is governed by that service's terms of use, privacy policy and any agreements you have with it (SLA, DPA), not by MeetRec. Check them before using it for confidential meetings."
- Prima di salvare la prima key di un servizio: avviso "The meeting text (not the audio) will be sent to <servizio> to create the summary.", con la stessa precisazione sul trattamento dei dati e i bottoni "Continue" e "Cancel".
- Automatic Summary è spento all'installazione. Attivandolo compare la scelta del modello tra quelli con key, **senza preselezione**: l'interruttore resta spento finché l'utente non sceglie un modello.
- La lingua di risposta in modalità automatica è sempre Auto e non si sceglie qui.
