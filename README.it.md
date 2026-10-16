[🇬🇧 English](README.md) · **🌐 Italiano**

# DocClassifier — gestore documentale personale in Python

![Home di DocClassifier](docs/screenshot-home.png)

`DocClassifier` (pacchetto Python: `devon`) indicizza le tue cartelle di
documenti in un database SQLite con ricerca full-text, tag, gruppi
(anche "smart"), **categorie semantiche con ancoraggi positivi/negativi**,
suggerimenti "vedi anche", rilevamento dei duplicati ed export verso un
disco esterno.

**Non modifica, sposta o cancella mai i file originali**: legge, indicizza
e al massimo *copia* (con `export`).

Funziona con la sola libreria standard di Python 3.10+ (testato fino a
3.13), quindi gira senza installare nulla.

## Ricerca alla Google con parole chiave (vettoriale)

La ricerca principale è **vettoriale**: ogni termine contribuisce al
vettore «da cercare» con segno e peso.

```
ricetta +#mamma +#carciofi -#pomodoro
2*assicurazione
```

- `+termine` avvicina, `-termine` allontana, `peso*termine` lo pesa;
- `#nome` espande una **parola chiave**: un vettore composto che definisci
  tu, unendo frasi, altre parole chiave e documenti campione:

```
mamma
mama
1.0*"madre"
1.0*#famiglia
-0.5*"fratello"
0.5*"D:\foto\mamma.jpg"     ← documento campione con valore
```

Un documento appartiene ai risultati se il suo blocco di contenuto è
simile al vettore composto (i documenti lunghi sono spezzati in più
blocchi: basta che ne sia vicino uno). Le corrispondenze lessicali
esatte — fondamentali per i nomi propri — aggiungono un bonus con
una gerarchia: parola presente → parole adiacenti → presenti nel nome
del file.

Le **categorie pronte** (famiglia, salute, lavoro, finanza, casa,
veicoli, viaggi, identità, scuola, hobby) si creano in un clic dal
pannello «⚙ Parole chiave» e compaiono come tag cliccabili. Il **filtro
formati** (.doc, .docx, .pdf…) si seleziona sotto la ricerca ed è
memorizzato nel browser.

![Ricerca vettoriale](docs/screenshot-ricerca.png)

![Parole chiave e categorie pronte](docs/screenshot-categorie.png)

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

## Installazione da sorgente

```bash
cd DocClassifier               # la cartella di questo progetto
python3 -m devon init          # crea ~/.devon/library.db
```

Opzionale, per estrarre il testo dai PDF:

```bash
pip3 install --user pypdf
```

## Uso tipico

```bash
# 1. indicizza le cartelle con i tuoi documenti
python3 -m devon import ~/Documents ~/Desktop ~/Downloads

# 2. cerca e esplora
python3 -m devon search "consiglio amministrazione"
python3 -m devon search "bilancio" kind:pdf
python3 -m devon similar 42

# 3. organizza con tag, gruppi e parole chiave
python3 -m devon tag 42 +personale
python3 -m devon group add Personale
python3 -m devon group put Personale 42 43 44
python3 -m devon group add "Consiglio Alfa" --query "alfa consiglio verbale"

# 4. duplicati ed export
python3 -m devon dupes
python3 -m devon export --tag personale --dest /Volumes/Backup/personale

# interfaccia grafica web (apre il browser su http://127.0.0.1:8300)
python3 -m devon serve
```

## Le due interfacce

- **`/` — ricerca Google-like** (quella consigliata): logo, casella di
  ricerca, tag cliccabili, filtro formati con memoria nel browser,
  indicatore di stato dell'indicizzazione in basso a sinistra
  (documenti, embedding, scansione in corso). Da smartphone si adatta
  da sola.
- **`/classic` — interfaccia completa a tre pannelli**: barra laterale
  (tipi, argomenti, concetti, gruppi, tag), ricerca live con snippet,
  scheda documento con azioni (apri/scarica file, tag, gruppi, esempi
  A/B per i concetti), pannello **Cartelle e indice** (sorgenti
  permanenti, aggiornamento incrementale di file nuovi e modificati con
  stato in tempo reale), **Analisi semantica** (embedding e clustering),
  **Concetti** (classificatore A/B), vista Duplicati e statistiche.

Avvio: `python3 -m devon serve` (opzioni: `--port 8300 --host
127.0.0.1 --no-browser`).

## Ricerca semantica e classificazione automatica

- **Embedding a blocchi** — i documenti con testo vengono spezzati in
  passaggi da ~1000 caratteri e TUTTO il contenuto viene vettorizzato
  (modello multilingue locale via `fastembed`, numpy per i calcoli);
  un documento risulta affine se lo è una sua parte.
- **Ricerca semantica** — casella «semantica» nella web gui classica o
  comando `semsearch`: ordina per affinità di significato; i filtri
  (`kind:`, `tag:`…) restano attivi.
- **Concetti (classificatore A/B)** — ancoraggi positivi e negativi
  (frasi, documenti campione per percorso, tipi file) con punteggio
  `sim(XX,A) − sim(XX,B)`; combinabili con `@Nome`. Filtro per tipi
  facoltativo («solo questi tipi»).
- **Argomenti (clustering)** — k-means per contenuti con etichetta
  automatica, filtrabile con `topic:"etichetta"`.
- **Categorie semantiche** — gruppi smart con query `sem:...` (unione
  di corrispondenze lessicali e semantiche).

Comandi: `semsearch QUERY` · `embed` · `cluster [-k N] [--all-kinds]` ·
`topics` · `similar ID` · `classify [ID] [--all] [--apply]`

## Sintassi di ricerca (interfaccia classica)

| Esempio                      | Effetto                                    |
|------------------------------|--------------------------------------------|
| `verbale assemblea`          | documenti con entrambe le parole           |
| `consigl*`                   | prefisso (consiglio, consigliere…)         |
| `-bozza`                     | esclude i documenti con "bozza"            |
| `tag:personale`              | solo documenti con quel tag                |
| `kind:pdf` `kind:email`      | filtro per tipo                            |
| `ext:.docx`                  | filtro per estensione                      |
| `path:consigli`              | filtro sul percorso                        |
| `group:personale`            | dentro un gruppo                           |
| `topic:"07 · verbale"`       | dentro un argomento automatico             |

Gli accenti non sono un problema: `perché` trova anche `perche`.

## Comandi

```
init            crea/apre il database
import PATH…    indicizza cartelle o file (incrementale: rileva nuovi e modificati)
ls              elenca documenti (--kind --tag --group --json)
search QUERY    ricerca full-text con snippet evidenziato
show ID         dettagli, testo, tag, gruppi, vedi-anche
rm ID…          toglie dall'INDICE (i file restano al loro posto)
tag ID +t -t    aggiunge/rimuove tag
tags            elenca i tag in uso
group add|ls|rm|put   gruppi normali e smart (--query "sem:…")
similar ID      documenti affini (embedding o TF-IDF)
semsearch QUERY ricerca semantica
embed           calcola gli embedding (incrementale)
cluster [-k N]  raggruppa per contenuti in argomenti
topics          elenca gli argomenti
classify [ID]   suggerisce/assegna i gruppi per contenuto
dupes           gruppi di duplicati (stesso contenuto, SHA-256)
stats           statistiche dell'archivio
check           file indicizzati ma non più presenti su disco
export          copia documenti (per query, tag, gruppo o ID) in una cartella
serve           web gui (default porta 8300, apre il browser)
```

Opzione globale `--db PERCORSO` (o variabile d'ambiente `DEVON_DB`).

## Formati supportati

- **Testo diretto**: txt, md, csv, json, xml, html, rtf, codice sorgente
- **Office (zip/XML)**: docx, xlsx, pptx, odt, ods, odp, epub — senza
  dipendenze esterne
- **Office storici binari**: doc, xls, ppt — estrazione euristica del
  testo (cp1252 e UTF-16) direttamente dai byte
- **Email**: .eml / .emlx (intestazioni + corpo)
- **PDF**: testo con `pypdf` (opzionale); OCR con `pytesseract` +
  Tesseract (opzionale) per le scansioni
- **Immagini**: OCR opzionale come sopra
- **Tutti gli altri formati**: indicizzati con metadati (nome, percorso,
  data, hash) e ricercabili per nome

## Windows

Due launcher pronti: `avvia-devon-web.bat` (web gui su 0.0.0.0:8300 con
database in `D:\devon\library.db`) e `importa-C-D.bat` (indicizza C:\ e
D:\ saltando le aree di sistema). Per l'accesso da altri PC della LAN:

```bat
netsh advfirewall firewall add rule name="devon web 8300" dir=in action=allow protocol=TCP localport=8300
```

(da terminale **come amministratore**, una volta sola)

## Dove stanno i miei dati?

Tutto in un unico file SQLite (`~/.devon/library.db`, oppure il percorso
`--db`/`DEVON_DB`): backup = copia del file; ripartenza da zero =
cancellazione; indice sempre ricostruibile rilanciando `import`.

## Struttura del progetto

```
devon/
├── db.py        schema SQLite (documenti, FTS5, tag, gruppi,
│                parole chiave, concetti, embedding) + migrazioni
├── extract.py   estrazione testo per formato (Office, PDF, email…)
├── ingest.py    scansione ricorsiva, hash SHA-256, sorgenti
├── search.py    parser query + ricerca FTS5 con snippet
├── semantic.py  embedding a blocchi, ricerca vettoriale, clustering,
│                concetti A/B
├── similar.py   vedi-anche (TF-IDF / embedding)
├── cli.py       interfaccia a riga di comando
└── webapp.py    web gui (API JSON + due interfacce, stdlib only)
```

---

[🇬🇧 English](README.md) · **🌐 Italiano**
