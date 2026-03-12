#!/usr/bin/env python3
"""
B2B Order Monitor
Surveille les commandes sur 3 comptes et envoie des notifications email.
"""

import json
import os
import smtplib
import hashlib
import logging
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
# Toutes les valeurs sensibles viennent des variables d'environnement GitHub Actions

ACCOUNTS = [
    {
        "name":     os.environ["ACCOUNT1_NAME"],   # ex: "Compte Commercial A"
        "url":      os.environ["B2B_URL"],
        "username": os.environ["ACCOUNT1_USER"],
        "password": os.environ["ACCOUNT1_PASS"],
    },
    {
        "name":     os.environ["ACCOUNT2_NAME"],
        "url":      os.environ["B2B_URL"],
        "username": os.environ["ACCOUNT2_USER"],
        "password": os.environ["ACCOUNT2_PASS"],
    },
    {
        "name":     os.environ["ACCOUNT3_NAME"],
        "url":      os.environ["B2B_URL"],
        "username": os.environ["ACCOUNT3_USER"],
        "password": os.environ["ACCOUNT3_PASS"],
    },
]

SMTP_CONFIG = {
    "host":     os.environ["SMTP_HOST"],       # ex: mail.mondomaine.fr
    "port":     int(os.environ.get("SMTP_PORT", "587")),
    "user":     os.environ["SMTP_USER"],
    "password": os.environ["SMTP_PASS"],
    "from":     os.environ["SMTP_FROM"],       # expéditeur
    "to":       os.environ["NOTIFY_EMAIL"],    # destinataire (vous)
}

# Fichier JSON qui garde en mémoire les commandes déjà vues
STATE_FILE = "orders_state.json"

# ── Sélecteurs CSS ─────────────────────────────────────────────────────────
# ⚠️  À ADAPTER selon le vrai site B2B ⚠️
SELECTORS = {
    "login": {
        "username_field": "input[name='username'], input[type='email'], #login",
        "password_field": "input[name='password'], input[type='password'], #password",
        "submit_button":  "button[type='submit'], input[type='submit'], .btn-login",
    },
    "orders": {
        # Chemin vers la page des commandes (relatif à B2B_URL)
        "orders_page_path": "/orders",           # ← modifier si besoin
        # Lignes du tableau
        "order_rows":       "table tbody tr, .order-list .order-item",
        # Colonnes dans chaque ligne
        "order_id":     "td:nth-child(1), .order-id",
        "client_name":  "td:nth-child(2), .client-name",
        "amount":       "td:nth-child(3), .order-amount",
        "quantity":     "td:nth-child(4), .order-qty",
        "order_date":   "td:nth-child(5), .order-date",
    },
}


# ── State helpers ─────────────────────────────────────────────────────────────

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def order_hash(order: dict) -> str:
    key = f"{order.get('id','')}-{order.get('client','')}-{order.get('amount','')}"
    return hashlib.md5(key.encode()).hexdigest()


# ── Scraping ──────────────────────────────────────────────────────────────────

def scrape_orders(account: dict) -> list:
    """Se connecte au compte et retourne la liste des commandes visibles."""
    orders = []
    s = SELECTORS

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        try:
            # 1. Connexion
            log.info(f"[{account['name']}] Connexion à {account['url']} …")
            page.goto(account["url"], timeout=30_000)
            page.fill(s["login"]["username_field"], account["username"])
            page.fill(s["login"]["password_field"], account["password"])
            page.click(s["login"]["submit_button"])
            page.wait_for_load_state("networkidle", timeout=20_000)

            # 2. Navigation vers les commandes
            orders_url = account["url"].rstrip("/") + s["orders"]["orders_page_path"]
            log.info(f"[{account['name']}] Accès commandes : {orders_url}")
            page.goto(orders_url, timeout=30_000)
            page.wait_for_load_state("networkidle", timeout=20_000)

            # 3. Extraction des lignes
            rows = page.query_selector_all(s["orders"]["order_rows"])
            log.info(f"[{account['name']}] {len(rows)} ligne(s) trouvée(s)")

            for row in rows:
                def txt(sel):
                    el = row.query_selector(sel)
                    return el.inner_text().strip() if el else ""

                order = {
                    "id":       txt(s["orders"]["order_id"]),
                    "client":   txt(s["orders"]["client_name"]),
                    "amount":   txt(s["orders"]["amount"]),
                    "quantity": txt(s["orders"]["quantity"]),
                    "date":     txt(s["orders"]["order_date"]),
                    "account":  account["name"],
                }
                if order["id"] or order["client"]:   # ignorer les lignes vides
                    orders.append(order)

        except PlaywrightTimeout as e:
            log.error(f"[{account['name']}] Timeout : {e}")
        except Exception as e:
            log.error(f"[{account['name']}] Erreur : {e}")
        finally:
            browser.close()

    return orders


# ── Email ─────────────────────────────────────────────────────────────────────

def send_email(new_orders: list):
    if not new_orders:
        return

    subject = (
        f"🛒 {len(new_orders)} nouvelle(s) commande(s) B2B – "
        f"{datetime.now().strftime('%d/%m/%Y %H:%M')}"
    )

    rows_html = ""
    for o in new_orders:
        rows_html += f"""
        <tr>
          <td style="padding:8px;border:1px solid #ddd">{o['account']}</td>
          <td style="padding:8px;border:1px solid #ddd"><strong>{o['client']}</strong></td>
          <td style="padding:8px;border:1px solid #ddd">{o['amount']}</td>
          <td style="padding:8px;border:1px solid #ddd">{o['quantity']}</td>
          <td style="padding:8px;border:1px solid #ddd">{o['date']}</td>
        </tr>"""

    html = f"""
    <html><body style="font-family:Arial,sans-serif;color:#333;max-width:700px;margin:auto">
      <div style="background:#2563eb;padding:20px;border-radius:8px 8px 0 0">
        <h2 style="color:white;margin:0">🛒 Nouvelles commandes B2B</h2>
      </div>
      <div style="border:1px solid #e5e7eb;border-top:none;padding:20px;border-radius:0 0 8px 8px">
        <p><strong>{len(new_orders)} nouvelle(s) commande(s)</strong> détectée(s) :</p>
        <table style="border-collapse:collapse;width:100%;font-size:14px">
          <thead>
            <tr style="background:#f1f5f9">
              <th style="padding:10px;border:1px solid #ddd;text-align:left">Compte</th>
              <th style="padding:10px;border:1px solid #ddd;text-align:left">Client</th>
              <th style="padding:10px;border:1px solid #ddd;text-align:left">Montant</th>
              <th style="padding:10px;border:1px solid #ddd;text-align:left">Quantité</th>
              <th style="padding:10px;border:1px solid #ddd;text-align:left">Date</th>
            </tr>
          </thead>
          <tbody>{rows_html}</tbody>
        </table>
        <p style="margin-top:20px;font-size:12px;color:#9ca3af">
          Envoyé automatiquement · {datetime.now().strftime('%d/%m/%Y à %H:%M')}
        </p>
      </div>
    </body></html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_CONFIG["from"]
    msg["To"]      = SMTP_CONFIG["to"]
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP(SMTP_CONFIG["host"], SMTP_CONFIG["port"]) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_CONFIG["user"], SMTP_CONFIG["password"])
            server.sendmail(SMTP_CONFIG["from"], SMTP_CONFIG["to"], msg.as_string())
        log.info(f"✅ Email envoyé : {subject}")
    except Exception as e:
        log.error(f"❌ Échec envoi email : {e}")
        raise


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("=== Démarrage B2B Order Monitor ===")
    state = load_state()
    all_new_orders = []

    for account in ACCOUNTS:
        orders = scrape_orders(account)
        account_key = account["name"]
        known_hashes = set(state.get(account_key, []))
        new_orders = []

        for order in orders:
            h = order_hash(order)
            if h not in known_hashes:
                new_orders.append(order)
                known_hashes.add(h)

        if new_orders:
            log.info(f"[{account['name']}] 🆕 {len(new_orders)} nouvelle(s) commande(s)")
            all_new_orders.extend(new_orders)
        else:
            log.info(f"[{account['name']}] Aucune nouvelle commande")

        state[account_key] = list(known_hashes)

    if all_new_orders:
        send_email(all_new_orders)
    else:
        log.info("Aucune nouvelle commande sur l'ensemble des comptes.")

    save_state(state)
    log.info("=== Fin du contrôle ===")


if __name__ == "__main__":
    main()
