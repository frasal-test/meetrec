# Scenari di test obbligatori

Tutti i test usano adapter fake e storage controllabile. Nessuna rete, nessuna key reale.

## Check-up e salvataggio
| # | Scenario | Esito atteso |
|---|---|---|
| T1 | Template `es` mancante | controllo all'avvio fallisce, il flusso non parte |
| T2 | Modello senza prezzo, senza `max_output_tokens` o senza `context_window` | controllo all'avvio fallisce |
| T3 | File temporaneo orfano presente | il controllo all'avvio lo rimuove |
| T4 | Salvataggio transcript fallisce | END_FATAL; nessuna schermata; nessuna chiamata |
| T5 | Due esecuzioni sullo stesso transcript | due risultati distinti (`run_id` diversi), nessuna sovrascrittura |

## Prima della chiamata
| # | Scenario | Esito atteso |
|---|---|---|
| T6 | Transcript di 149 parole | END_NO_LLM `too_short`; nessuna schermata |
| T7 | Transcript di 150 parole | prosegue a CHECK_KEYS |
| T8 | Nessuna key | END_NO_LLM `no_keys`; nessuna schermata |
| T9 | Una sola LLM | ASK_CHOICE mostra "Nessuno" + quella LLM, nessuna preselezionata |
| T10 | Più LLM, due esecuzioni consecutive | in entrambe nessuna preselezione, né modello né lingua |
| T11 | Utente sceglie "Nessuno" | END_NO_LLM `declined`; nessuna chiamata |
| T12 | Stima costo | valore = formula di `adapter-contract.md` per input noto, in USD |

## Prompt
| # | Scenario | Esito atteso |
|---|---|---|
| T13 | Transcript `it` / `en` / `es` | template corrispondente |
| T14 | Transcript `auto` | template inglese |
| T15 | Metadato lingua assente | template inglese + riga di log |
| T16 | Risposta Ita/Eng/Esp | istruzione "Respond in <lingua>" nel prompt |
| T17 | Risposta Auto | istruzione "Respond in the same language as the transcript" |
| T18 | Transcript contenente "ignora le istruzioni precedenti" | testo dentro i delimitatori, system prompt con regola I8 |

## Chiamata ed errori
| # | Scenario | Esito atteso |
|---|---|---|
| T19 | Successo `complete` | DONE; risultato con tutti i campi di I14; costo corretto |
| T20 | Successo `truncated` | DONE con stato `truncated`; messaggio dedicato |
| T21 | Modello assente dalla tabella prezzi | `cost.amount is None`, non 0 |
| T22 | Timeout al 1° tentativo | ASK_RETRY mostrata |
| T23 | Timeout, Riprova, successo | DONE; due tentativi nel risultato, costo totale = somma |
| T24 | Timeout, Riprova, timeout | END_ERROR; nessuna terza schermata |
| T25 | Timeout, Esci | END_ERROR |
| T26 | Fake OpenAI: 429 con `insufficient_quota` | `InsufficientCreditsError`; nessuna ASK_RETRY |
| T27 | Fake OpenAI: 429 per rate limit | `RateLimitError`; ASK_RETRY |
| T28 | Fake Anthropic: 400 per credito esaurito | `InsufficientCreditsError` |
| T29 | AuthError, ModelNotFoundError, ContentFilteredError | END_ERROR diretto, messaggio dedicato |
| T30 | Eccezione imprevista nell'SDK | `UnknownLLMError`; END_ERROR; nessun crash |
| T31 | Errore con usage parziale | costo del tentativo registrato |
| T32 | Client SDK creati dagli adapter | `max_retries == 0` e timeout impostati |

## Salvataggio risultato e trasversali
| # | Scenario | Esito atteso |
|---|---|---|
| T33 | SAVE_RESULT fallisce | file di emergenza scritto; END_SAVE_ERROR; percorso nel messaggio |
| T34 | Isolamento SDK | nessun modulo fuori dagli adapter importa SDK; nessun import SDK all'avvio |
| T35 | Key fittizia riconoscibile | non compare in log, errori, risultati, messaggi |
| T36 | Log | una riga per transizione con `run_id` |
| T37 | Esiti | i cinque esiti restituiscono valori distinti |

## Modalità automatica, contesto e controlli
| # | Scenario | Esito atteso |
|---|---|---|
| T38 | Automatica, default con key e contesto sufficiente | nessuna ASK_CHOICE; richiesta con il modello di default e "Respond in the same language as the transcript" |
| T39 | Automatica, default con key ma transcript troppo lungo | END_NO_LLM `too_long`; nessuna schermata; nessuna chiamata |
| T40 | Automatica, default senza key, un'altra LLM con key | ASK_CHOICE senza preselezione; riga di log |
| T41 | Manuale, un modello con contesto insufficiente | in ASK_CHOICE appare disattivato e non è selezionabile |
| T42 | Key presenti ma transcript troppo lungo per tutti i modelli | END_NO_LLM `too_long`; nessuna schermata |
| T43 | Automatica, errore ritentabile al 1° tentativo | ASK_RETRY mostrata (nessun retry automatico) |
| T44 | Controllo all'avvio | nessuna chiamata di rete; il controllo Hugging Face c'è solo nel `doctor` completo |
| T45 | SAVE_TRANSCRIPT fallisce a ogni tentativo del job | END_FATAL comunicato solo dopo l'ultimo tentativo; nessuna chiamata LLM |
| T46 | Settings: attivazione di Automatic Summary | nessun modello preselezionato; senza scelta resta spento |
| T47 | `is_configured` | legge solo il Portachiavi (fake nei test); nessun import SDK, nessuna rete |
