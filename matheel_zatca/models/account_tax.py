# ZATCA's VAT category on each tax: standard 15% is automatic, zero-rated / exempt / out-of-scope need a reason code.
from odoo import fields, models


class AccountTax(models.Model):
    _inherit = "account.tax"

    matheel_tax_category = fields.Selection(
        [("S", "Standard (S)"), ("Z", "Zero-rated (Z)"), ("E", "Exempt (E)"), ("O", "Out of scope (O)")],
        "ZATCA category",
        help="Leave empty for a standard 15% tax.",
    )
    matheel_exemption_code = fields.Char(
        "ZATCA exemption code", help="For Z/E/O, e.g. VATEX-SA-32 (exports) or VATEX-SA-29 (financial services)."
    )
