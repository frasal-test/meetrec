# Istruzioni per Claude Code

## Contesto
Questa specifica descrive il flusso di elaborazione LLM di un transcript **all'interno di un'app esistente**. L'app non gira in un terminale: l'interazione con l'utente avviene solo tramite menu e bottoni. Esiste già una funzione `doctor` che verifica lo stato dell'app.

## Flusso vincolante
La specifica in `docs/flow/` è **vincolante**. Prima di scrivere o modificare codice che tocca transcript, scelta LLM, prompt, chiamate ai provider o salvataggio dei risultati, leggi:

1. `docs/flow/flow.md` — diagramma, stati, transizioni, invarianti
2. `docs/flow/adapter-contract.md` — contratto degli adapter, errori normalizzati, costi
3. `docs/flow/ui.md` — schermate e messaggi verso l'utente
4. `docs/flow/test-scenarios.md` — scenari di test obbligatori

## Primo passo
Prima di implementare, confronta il codice esistente con la specifica: indica quali stati/invarianti sono già coperti, quali parzialmente, quali mancano. Presenta l'elenco all'utente e attendi conferma prima di procedere.

## Regole
- Non aggiungere, rimuovere o riordinare stati e transizioni. Se una richiesta implica una modifica al flusso, **fermati e segnalalo prima di implementare**, indicando quale stato/invariante cambierebbe.
- Ogni invariante in `flow.md` deve essere rispettato. Se non riesci a rispettarne uno, dillo esplicitamente.
- I punti marcati **DA DEFINIRE** non vanno decisi da te: chiedi all'utente.
- Nessuna chiamata a SDK/API dei provider fuori dagli adapter.
- Nessun default o preselezione dove la specifica prevede una scelta dell'utente.
- Ogni scenario in `test-scenarios.md` deve avere almeno un test automatico. Esegui i test dopo ogni modifica che tocca il flusso e riporta l'esito.
- Nei test gli adapter reali sono sostituiti da fake; nessun test usa API key reali o la rete.
- Ogni funzione che implementa una parte del flusso indica nel docstring lo stato o la transizione corrispondente (es. `Stato: ASK_CHOICE`).

## Nuove funzionalità
L'utente descriverà la logica dell'app. Mappa ogni funzione che scrivi su uno stato o una transizione del flusso.
