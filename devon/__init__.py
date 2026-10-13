"""devon — piccolo gestore documentale personale ispirato a DEVONthink.

Indicizza cartelle di documenti in un database SQLite con ricerca
full-text, tag, gruppi (anche "smart"), suggerimenti "vedi anche"
(TF-IDF), rilevamento duplicati ed export. Non modifica mai i file
originali: lavora solo su copie e metadati.
"""

__version__ = "0.1.0"
