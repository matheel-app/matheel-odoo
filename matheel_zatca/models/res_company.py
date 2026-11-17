# The company's Matheel connection: key, environment, and the device token that carries its ZATCA invoice chain.
import requests

from odoo import _, fields, models
from odoo.exceptions import UserError

MATHEEL_API = "https://matheel.app/api/v1"


class ResCompany(models.Model):
    _inherit = "res.company"

    matheel_api_key = fields.Char("Matheel key", groups="base.group_system")
    matheel_environment = fields.Selection(
        [("sandbox", "Sandbox (test, free)"), ("simulation", "Simulation"), ("production", "Production")],
        default="sandbox",
        required=True,
    )
    matheel_legal_name_ar = fields.Char("Legal name (Arabic)", help="As registered with ZATCA. Defaults to the company name.")
    matheel_device_token = fields.Char(groups="base.group_system", copy=False)
    matheel_device_serial = fields.Char("ZATCA device serial", readonly=True, copy=False)
    matheel_certificate_expires_at = fields.Char("Certificate expires", readonly=True, copy=False)

    def _matheel_request(self, path, body):
        """POSTs to the Matheel API and returns (http_status, decoded_json); network errors come back as status 0."""
        self.ensure_one()
        headers = {"Content-Type": "application/json", "User-Agent": "matheel-odoo/17.0.1.0.0"}
        if self.sudo().matheel_api_key:
            headers["Authorization"] = "Bearer " + self.sudo().matheel_api_key.strip()
        try:
            res = requests.post(MATHEEL_API + path, json=body, headers=headers, timeout=90)
        except requests.RequestException as e:
            return 0, {"error": "network", "detail": str(e)}
        try:
            return res.status_code, res.json()
        except ValueError:
            return res.status_code, {"error": "bad_response", "detail": res.text[:300]}

    def _matheel_seller(self):
        """The seller block ZATCA needs, from the company record."""
        self.ensure_one()
        p = self.partner_id
        return {
            "vat_number": (self.vat or "").strip(),
            "legal_name_ar": self.matheel_legal_name_ar or self.name,
            "legal_name_en": self.name,
            "crn": (self.company_registry or "").strip() or None,
            "address": {
                "street": p.street or "",
                "building_number": p.matheel_building_number or "",
                "district": p.street2 or "",
                "city": p.city or "",
                "postal_code": p.zip or "",
            },
        }

    def matheel_onboard(self, otp=None):
        """Registers this company as a ZATCA device (EGS) through Matheel and stores the device token."""
        self.ensure_one()
        if self.matheel_environment != "sandbox" and not otp:
            raise UserError(_("Enter the OTP from the Fatoora portal (fatoora.zatca.gov.sa) | أدخل رمز OTP من بوابة فاتورة"))
        body = {
            "environment": self.matheel_environment,
            "seller": self._matheel_seller(),
            "device": {"name": "Odoo " + self.env.cr.dbname, "invoice_types": ["standard", "simplified"]},
        }
        if otp:
            body["otp"] = otp.strip()
        status, res = self._matheel_request("/onboarding", body)
        if status != 200:
            raise UserError(_("Matheel %(status)s: %(detail)s", status=status, detail=res))
        self.sudo().write(
            {
                # ZATCA computes VAT on each category's total (BR-CO-14/15), not per line: round the same way, so
                # Odoo's totals equal the legal invoice's to the halala.
                "tax_calculation_rounding_method": "round_globally",
                "matheel_device_token": res["device_token"],
                "matheel_device_serial": (res.get("device") or {}).get("serial"),
                "matheel_certificate_expires_at": (res.get("device") or {}).get("certificate_expires_at"),
            }
        )

    def matheel_renew(self, otp):
        """Renews the device certificate with a fresh OTP; the invoice chain carries over."""
        self.ensure_one()
        status, res = self._matheel_request(
            "/onboarding/renew", {"device_token": self.sudo().matheel_device_token, "otp": (otp or "").strip()}
        )
        if status != 200:
            raise UserError(_("Matheel %(status)s: %(detail)s", status=status, detail=res))
        self.sudo().write(
            {
                "matheel_device_token": res["device_token"],
                "matheel_certificate_expires_at": (res.get("device") or {}).get("certificate_expires_at"),
            }
        )
