# DocClassifier — gestore documentale personale (ispirato a DEVONthink, in Python)

`DocClassifier` (pacchetto Python: `devon`) replica le funzioni essenziali
di DEVONthink: indicizza le tue cartelle di documenti in un database
SQLite con ricerca full-text, tag, gruppi (anche "smart"), **categorie
semantiche con ancoraggi positivi/negativi**, suggerimenti "vedi anche",
rilevamento dei duplicati ed export verso un disco esterno.

**Non modifica, sposta o cancella mai i file originali**: legge, indicizza
e al massimo *copia* (con `export`).

Funziona con la sola libreria standard di Python 3.10+ (testato fino a
3.12), quindi gira senza installare nulla anche su un Mac del 2013.

## Eseguibili pronti (senza installare Python)

GitHub Actions compila automaticamente i binari autonomi per
**Windows (x64)**, **macOS Intel** e **macOS Apple Silicon**: scheda
*Actions* → workflow "Build" → esegui oppure scarica l'artefatto della
tua piattaforma (spingendo un tag `v…` viene creata anche una Release).

```bash
# avvio dell'interfaccia web (raggiungibile in rete locale)
devon.exe --db D:\archivio\library.db serve --host 0.0.0.0 --port 8300
./devon     --db ~/archivio/library.db serve --host 0.0.0.0 --port 8300
```

Su macOS il binario non è firmato: al primo avvio usare tasto destro →
"Apri", oppure `xattr -d com.apple.quarantine devon`.

---

## Installazione su macOS

```bash
# Python 3 (se assente):  xcode-select --install   (oppure python.org)
cd devon                      # la cartella di questo progetto
python3 -m devon init         # crea ~/.devon/library.db
```

Opzionale, per estrarre il testo dai PDF:

```bash
pip3 install --user pypdf
```

Verifica rapida del prerequisito full-text:

```bash
python3 -c "import sqlite3; sqlite3.connect(':memory:').execute(
  \"CREATE VIRTUAL TABLE t USING fts5(x)\"); print('FTS5 ok')"
```

## Uso tipico (estrazione documenti prima della riconsegna del Mac)

```bash
# 1. indicizza le cartelle con i tuoi documenti
python3 -m devon import ~/Documents ~/Desktop ~/Downloads

# 2. trova i tuoi file e verificane il contenuto con snippet e "vedi anche"
python3 -m devon search "consiglio amministrazione"
python3 -m devon search "bilancio" kind:pdf
python3 -m devon similar 42
python3 -m devon show 42

# 3. organizza con tag e gruppi
python3 -m devon tag 42 +personale +consiglio-alfa-srl
python3 -m devon group add Personale
python3 -m devon group put Personale 42 43 44
python3 -m devon group add "Consiglio Alfa" --query "alfa consiglio verbale"

# 4. duplicati: trova le copie ridondanti
python3 -m devon dupes

# 5. esporta su disco esterno quello che ti serve
python3 -m devon export --tag personale --dest /Volumes/Backup/personale
python3 -m devon export --group "Consiglio Alfa" --dest /Volumes/Backup/alfa

# interfaccia grafica web (apre il browser su http://127.0.0.1:8300)
python3 -m devon serve
```

## Ricerca semantica e classificazione automatica

devon sa anche cercare e organizzare **per significato**, non solo per
parola. Tutto il calcolo avviene in locale.

- **Ricerca semantica** — casella «semantica» nella web gui o comando
  `semsearch`: ordina i documenti per affinità di significato con la
  query. Con gli embedding neurali «rendiconto finanziario» trova anche
  «bilancio d'esercizio»; senza, un rango a similarità vettoriale
  TF-IDF fa da fallback. I filtri (kind:, tag:, path:…) restano attivi
  anche in modalità semantica.
- **Embedding a blocchi** — i tipi documento (PDF, Office, email,
  pagine web…) vengono spezzati in passaggi da ~1000 caratteri e
  TUTTO il contenuto viene vettorizzato: un documento risulta affine
  se lo è una sua parte (max per documento). Gli altri file hanno un
  vettore solo sull'inizio, e i file col solo nome non entrano nel
  rango semantico (restano trovabili per nome).
- **Categorie semantiche** — gruppi smart con query `sem:...`
  (es. `group add Honda --query "sem:honda moto"`): i membri uniscono
  le corrispondenze lessicali esatte e quelle semantiche sopra soglia
  (0.5). Nella web gui compaiono come gli altri gruppi ⚡.
- **Argomenti (clustering)** — «Analisi semantica → Raggruppa»: k-means
  sui vettori assegna a ogni documento un argomento con etichetta
  automatica, filtrabile con `topic:"etichetta"`.
- **Documenti simili** — nella scheda di ogni documento, gli affini
  per significato (max sui blocchi); da CLI `classify` suggerisce/assegna
  i gruppi per contenuto.

Comandi: `semsearch QUERY` · `embed` · `cluster [-k N]` · `topics` ·
`similar ID` · `classify [ID] [--all] [--apply]`

## Interfaccia grafica web

`python3 -m devon serve` apre nel browser un'interfaccia a tre pannelli
stile DEVONthink, senza alcuna dipendenza (HTML+CSS+JS inclusi
nell'applicazione):

- **Barra laterale**: tutti i documenti, filtro per tipo, gruppi
  (normali e ⚡ smart, cliccabili), tag con conteggi, vista Duplicati
- **Ricerca live** mentre digiti, con snippet evidenziato e chip dei tag
- **Scheda documento**: metadati, testo, "vedi anche"; pulsanti per
  **aprire il file originale** nel browser, scaricarlo, aggiungere o
  rimuovere tag e gruppi (anche creandone di nuovi al volo) e rimuovere
  il documento dall'indice
- **Cartelle e indice**: gestione permanente delle sorgenti — percorsi
  da indicizzare con numero di documenti e data dell'ultima scansione,
  aggiunta/rimozione, e «Aggiorna» (singola cartella o tutte) che
  rileva file **nuovi e modificati** in modo incrementale, con stato
  dell'indicizzazione in tempo reale (visti/nuovi/aggiornati/duplicati)

Opzioni: `--port 8300 --host 127.0.0.1 --no-browser`.

L'interfaccia è responsive: da smartphone (stesso indirizzo in rete
locale) la barra laterale diventa un menu a tendina con l'icona ☰ e le
schede documento si aprono a pieno schermo.

## Sintassi di ricerca

| Esempio                      | Effetto                                    |
|------------------------------|--------------------------------------------|
| `verbale assemblea`          | documenti con entrambe le parole           |
| `consigl*`                   | prefisso (consiglio, consigliere…)         |
| `-bozza`                     | esclude i documenti con "bozza"            |
| `tag:personale`              | solo documenti con quel tag                |
| `kind:pdf` `kind:email`      | filtro per tipo                            |
| `ext:.docx`                  | filtro per estensione                      |
| `path:consigli`              | filtro sul percorso                        |
| `group:personale`            | dentro un gruppo (incluse le sottocartelle)|

Gli accenti non sono un problema: `perché` trova anche `perche` e viceversa.

## Comandi

```
init            crea/apre il database
import PATH…    indicizza cartelle o file (ricalcolo incrementale)
ls              elenca documenti (--kind --tag --group --json)
search QUERY    ricerca full-text con snippet evidenziato
show ID         dettagli, testo, tag, gruppi, vedi-anche
rm ID…          toglie dall'INDICE (i file restano al loro posto)
tag ID +t -t    aggiunge/rimuove tag
tags            elenca i tag in uso
group add|ls|rm|put   gruppi normali e smart (--query)
similar ID      documenti affini (TF-IDF, similarità del coseno)
dupes           gruppi di duplicati (stesso contenuto, SHA-256)
stats           statistiche dell'archivio
check           file indicizzati ma non più presenti su disco
export          copia documenti (per query, tag, gruppo o ID) in una cartella
serve           interfaccia grafica web (default porta 8300, apre il browser)
```

Opzione globale `--db PERCORSO` (o variabile d'ambiente `DEVON_DB`) per
usare un database diverso da `~/.devon/library.db`.

## Formati supportati

- **Testo diretto**: txt, md, csv, json, xml, html, rtf, codice sorgente
- **Office (zip/XML)**: docx, **xlsx**, **pptx**, odt, ods, odp, epub —
  senza dipendenze esterne (fogli: stringhe e valori delle celle;
  presentazioni: testo delle slide in ordine)
- **Office storici binari**: **doc, xls, ppt** — estrazione euristica
  delle sequenze di testo (cp1252 e UTF-16) direttamente dai byte:
  i file diventano plenamente ricercabili senza librerie esterne
- **Email**: .eml / .emlx (intestazioni + corpo)
- **PDF**: testo con `pypdf` (opzionale); se il PDF è una scansione,
  OCR con `pytesseract` + Tesseract (opzionale)
- **Immagini**: OCR opzionale come sopra
- **Tutti gli altri formati**: indicizzati con metadati (nome, percorso,
  data, hash) e ricercabili per filename

## Windows

Il codice è identico: serve solo Python 3 per Windows (python.org).
Due launcher pronti nella cartella del progetto:

- `avvia-devon-web.bat` — avvia la web gui su **0.0.0.0:8300**
  (raggiungibile dagli altri PC della rete locale) con database in
  `D:\devon\library.db`
- `importa-C-D.bat` — indicizza **C:\ e D:\** in sequenza (le aree di
  sistema — Windows, Program Files, AppData, cestino — vengono saltate
  automaticamente)

Da riga di comando, l'equivalente manuale:

```bat
set DEVON_DB=D:\devon\library.db
cd /d C:\AI\src\devon
python -m devon serve --host 0.0.0.0 --port 8300
python -m devon import C:\ D:\
```

Per l'accesso da altri PC della LAN, se il firewall lo blocca, aprire
una volta sola un terminale **come amministratore**:

```bat
netsh advfirewall firewall add rule name="devon web 8300" dir=in action=allow protocol=TCP localport=8300
```

## Dove stanno i miei dati?

Tutto in `~/.devon/library.db` (SQLite + indice FTS5). Per fare un backup
dell'archivio basta copiare quel file. Per ripartire da zero basta
cancellarlo. L'indice è ricostruibile in ogni momento rilanciando
`import` sulle stesse cartelle.

## Struttura del progetto

```
devon/
├── db.py        schema SQLite (documenti, FTS5, tag, gruppi) + trigger
├── extract.py   estrazione testo per formato
├── ingest.py    scansione ricorsiva, hash SHA-256, duplicati
├── search.py    parser query + ricerca FTS5 con snippet
├── similar.py   vedi-anche (TF-IDF, similarità del coseno)
├── cli.py       interfaccia a riga di comando
└── webapp.py    interfaccia grafica web (API JSON + app, stdlib only)
```
