# Interfaccia utente

L'app non ha terminale: tutte le interazioni sono menu e bottoni. Due sole schermate di scelta nel flusso: ASK_CHOICE e ASK_RETRY. La finestra Settings è fuori dal flusso.

## ASK_CHOICE — schermata unica

```
┌──────────────────────────────────────────┐
│ Transcript salvato                       │
│ <nome file> · <lingua> · ~<N> parole     │
│                                          │
│ Elaborare con                            │
│ ( ) Nessuno              solo transcript │
│ ( ) <label modello 1>        max ~$0.09  │
│ ( ) <label modello 2>        max ~$0.06  │
│  -  <label modello 3>  transcript troppo │
│                        lungo             │
│                                          │
│ Lingua della risposta                    │
│ [Ita] [Eng] [Esp] [Auto]                 │
│                                          │
│              [ Avvia ]                   │
└──────────────────────────────────────────┘
```

- Nessuna opzione preselezionata (I5).
- Elenco modelli: solo quelli con key configurata, nell'ordine della tabella modelli.
- Accanto a ogni modello la stima di costo massimo in USD (vedi `adapter-contract.md`).
- Un modello con key ma senza contesto sufficiente (I17) appare disattivato, con "transcript troppo lungo", e non è selezionabile.
- Scelto "Nessuno": la lingua si disattiva, il bottone diventa "Chiudi".
- "Avvia" è attivo solo con modello + lingua scelti.
- Lingua del transcript `auto`: mostrata come "rilevamento automatico".

In modalità automatica ASK_CHOICE non viene mostrata, salvo il caso in cui il modello di default non abbia più la key (vedi `flow.md`).

## Durante la chiamata
Indicatore di attesa con il nome del modello. Nessuna interazione richiesta.

## ASK_RETRY
Mostrata solo per errori ritentabili al primo tentativo.

```
<messaggio errore per l'utente>
[ Riprova ]  [ Esci ]
```
Nessun default.

## Messaggi di esito

| Esito | Messaggio (indicativo) |
|---|---|
| DONE `complete` | Riassunto salvato in <percorso> · <modello> · <costo reale> |
| DONE `truncated` | Come sopra + "La risposta è stata troncata per limite di lunghezza" |
| END_NO_LLM `too_short` | Transcript troppo breve per un riassunto, salvato in <percorso> |
| END_NO_LLM `no_keys` | Transcript salvato. Nessun LLM configurato |
| END_NO_LLM `too_long` | Transcript troppo lungo per <modello / i modelli configurati>. Transcript salvato |
| END_NO_LLM `declined` | nessun messaggio, oppure solo conferma del salvataggio |
| END_ERROR `InsufficientCreditsError` | Credito esaurito su <provider>. Transcript salvato |
| END_ERROR `AuthError` | API key di <provider> non più valida. Transcript salvato |
| END_ERROR `ModelNotFoundError` | Modello non più disponibile presso <provider>: aggiornare l'app. Transcript salvato |
| END_ERROR `ContentFilteredError` | Il provider ha bloccato la risposta. Transcript salvato |
| END_ERROR altri | Errore durante l'elaborazione (<tipo>). Transcript salvato |
| END_SAVE_ERROR | Risposta salvata in emergenza in <percorso> |
| END_FATAL | Impossibile salvare il transcript (<motivo>) |

Nessun messaggio contiene dettagli tecnici grezzi o API key.

## Settings (fuori dal flusso)

```
┌──────────────────────────────────────────┐
│ API key                                  │
│ Anthropic   [ ••••••••  ] [Salva] ✓      │
│ OpenAI      [           ] [Salva]        │
│ Oracle OCI  [           ] Region [     ] │
│             [Salva]                      │
│                                          │
│ [ ] Automatic Summary                    │
│     Modello: ( ) <label 1>  ( ) <label 2>│
└──────────────────────────────────────────┘
```

- Le key vanno nel Portachiavi di macOS. Ogni key è verificata con una chiamata minima quando viene salvata (I4); se la verifica fallisce non viene salvata.
- Prima di salvare la prima key di un servizio: avviso "Il testo della riunione (non l'audio) verrà inviato a <servizio> per creare il riassunto."
- Automatic Summary è spento all'installazione. Attivandolo compare la scelta del modello tra quelli con key, **senza preselezione**: l'interruttore resta spento finché l'utente non sceglie un modello.
- La lingua di risposta in modalità automatica è sempre Auto e non si sceglie qui.
