# -*- coding: utf-8 -*-
"""Punto d'ingresso per l'eseguibile standalone (PyInstaller)."""
import sys

from devon.cli import main

if __name__ == "__main__":
    sys.exit(main())
