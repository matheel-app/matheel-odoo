# Settings → Accounting → ZATCA (Matheel): the key, environment and the one-button device registration.
from odoo import _, fields, models
from odoo.exceptions import UserError


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    matheel_api_key = fields.Char(related="company_id.matheel_api_key", readonly=False)
    matheel_environment = fields.Selection(related="company_id.matheel_environment", readonly=False)
    matheel_legal_name_ar = fields.Char(related="company_id.matheel_legal_name_ar", readonly=False)
    matheel_device_serial = fields.Char(related="company_id.matheel_device_serial")
    matheel_certificate_expires_at = fields.Char(related="company_id.matheel_certificate_expires_at")
    matheel_connected = fields.Boolean(compute="_compute_matheel_connected")
    matheel_otp = fields.Char("OTP")

    def _compute_matheel_connected(self):
        for s in self:
            s.matheel_connected = bool(s.company_id.sudo().matheel_device_token)

    def action_matheel_connect(self):
        """Saves the settings, then registers the company's device with ZATCA."""
        self.execute()
        self.company_id.matheel_onboard(self.matheel_otp)
        return self._matheel_reload(_("Device registered with ZATCA | تم تسجيل الجهاز"))

    def action_matheel_renew(self):
        """Renews the device certificate with the OTP entered above."""
        if not self.matheel_otp:
            raise UserError(_("Enter a new OTP from the Fatoora portal | أدخل رمز OTP جديد"))
        self.company_id.matheel_renew(self.matheel_otp)
        return self._matheel_reload(_("Certificate renewed | تم تجديد الشهادة"))

    def _matheel_reload(self, message):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"message": message, "type": "success", "next": {"type": "ir.actions.client", "tag": "reload"}},
        }
