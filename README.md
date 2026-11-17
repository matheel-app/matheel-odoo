# Matheel ZATCA connector for Odoo

Free ZATCA Phase 2 e-invoicing for Odoo (Community and Enterprise): every posted customer invoice and credit note is
cleared or reported to ZATCA through the [Matheel API](https://matheel.app), with ZATCA's XML, a bilingual PDF/A-3 and
the QR on Odoo's printed invoice. No certificates or crypto on your server.

ربط أودو بالفوترة الإلكترونية (المرحلة الثانية) مجاناً: كل فاتورة تُرحّل تنرسل للهيئة تلقائياً.

| Branch | Odoo |
|---|---|
| `17.0` | 17 |
| `18.0` | 18 |

## Install
Copy `matheel_zatca` into your addons path, update the apps list, install **ZATCA Phase 2 E-Invoicing (Matheel)**.
Then Settings → Accounting → *ZATCA e-invoicing (Matheel)* → Register device (Sandbox needs no key and no OTP).

## How it works
- Posting queues the invoice; a cron (triggered at once, then every 5 minutes) files the queue oldest first under a row
  lock on the company, and commits after each ZATCA answer, so the invoice chain (ICV/PIH, carried in Matheel's device
  token) never forks. Network errors, 409, 429 and 5xx are retried; ZATCA rejections show on the invoice.
- On registration the company switches to *Round globally*, the way ZATCA computes VAT, so totals match to the halala.

Pricing: the module is free (LGPL-3); live filing needs a Matheel plan (SAR 590/year per device). hello@matheel.app
