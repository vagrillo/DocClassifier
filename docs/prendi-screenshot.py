# -*- coding: utf-8 -*-
"""Screenshot dell'interfaccia DocClassifier per il README."""
from playwright.sync_api import sync_playwright

BASE = "http://192.168.1.174:8300"
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 840},
                            device_scale_factor=2)
    # home google-like
    page.goto(BASE + "/", wait_until="networkidle")
    page.wait_for_timeout(2000)
    page.screenshot(path="docs/screenshot-home.png")
    print("home OK")
    # ricerca con risultati (query d'esempio)
    page.fill("#q", "#polizza")
    page.press("#q", "Enter")
    page.wait_for_timeout(6000)
    page.screenshot(path="docs/screenshot-ricerca.png")
    print("ricerca OK")
    # modale parole chiave / categorie pronte
    page.goto(BASE + "/", wait_until="networkidle")
    page.click("text=Parole chiave")
    page.wait_for_timeout(1500)
    page.screenshot(path="docs/screenshot-categorie.png")
    print("categorie OK")
    browser.close()
