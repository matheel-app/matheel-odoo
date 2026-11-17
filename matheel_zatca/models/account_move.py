# Posting a customer invoice or credit note queues it; one cron files the queue in order, so the ZATCA chain never forks.
import base64
import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Statuses that mean ZATCA accepted the invoice; anything filed this way is never sent again.
DONE = ("reported", "cleared")
# Worth retrying later without changing anything: network, busy device, rate limit, ZATCA or Matheel down.
RETRY = (0, 409, 429, 500, 502, 503, 504)


class AccountMove(models.Model):
    _inherit = "account.move"

    matheel_state = fields.Selection(
        [("to_send", "Queued"), ("reported", "Reported"), ("cleared", "Cleared"), ("error", "Rejected")],
        "ZATCA",
        copy=False,
        readonly=True,
        tracking=True,
    )
    matheel_message = fields.Text("ZATCA message", copy=False, readonly=True)
    matheel_qr = fields.Char("ZATCA QR", copy=False, readonly=True)
    matheel_uuid = fields.Char("ZATCA UUID", copy=False, readonly=True)
    matheel_counter = fields.Integer("ZATCA counter (ICV)", copy=False, readonly=True)

    def _post(self, soft=True):
        posted = super()._post(soft)
        to_queue = posted.filtered(lambda m: m._matheel_applies() and m.matheel_state not in DONE)
        if to_queue:
            to_queue.write({"matheel_state": "to_send", "matheel_message": False})
            self.env.ref("matheel_zatca.ir_cron_matheel_send")._trigger()
        return posted

    def button_draft(self):
        """An invoice ZATCA accepted is final: correct it with a credit note. A queued one leaves the queue."""
        if self.filtered(lambda m: m.matheel_state in DONE):
            raise UserError(
                _(
                    "ZATCA has accepted this invoice, so it can't be edited. Issue a credit note instead."
                    " | الفاتورة اعتمدتها الهيئة ولا يمكن تعديلها، أصدر إشعار دائن."
                )
            )
        self.filtered(lambda m: m.matheel_state in ("to_send", "error")).write({"matheel_state": False})
        return super().button_draft()

    def _matheel_applies(self):
        """A customer invoice or credit note of a company that registered its ZATCA device."""
        self.ensure_one()
        return self.move_type in ("out_invoice", "out_refund") and bool(self.company_id.sudo().matheel_device_token)

    def action_matheel_send(self):
        """Queues the selected invoices again (after fixing a rejection) and runs the queue now."""
        for move in self:
            if move.state != "posted" or not move._matheel_applies():
                raise UserError(_("%s is not a posted customer invoice of a company connected to ZATCA.", move.name))
        self.filtered(lambda m: m.matheel_state not in DONE).write({"matheel_state": "to_send"})
        self.env.ref("matheel_zatca.ir_cron_matheel_send")._trigger()

    @api.model
    def _cron_matheel_send(self, limit=200):
        """Files queued invoices oldest first, one company chain at a time, committing after each ZATCA answer."""
        moves = self.search([("matheel_state", "=", "to_send"), ("state", "=", "posted")], order="id", limit=limit)
        for company in moves.company_id:
            # A row lock on the company: a second worker running this cron waits instead of reusing the token.
            self.env.cr.execute("SELECT id FROM res_company WHERE id = %s FOR UPDATE", [company.id])
            for move in moves.filtered(lambda m, c=company: m.company_id == c):
                if not move._matheel_file():
                    break  # retryable: keep the order, try the rest of this company's queue next run
                self.env.cr.commit()  # the new device token must survive anything that happens after

    def _matheel_file(self):
        """Sends one invoice; returns False when it should be retried later (nothing changed)."""
        self.ensure_one()
        company = self.company_id.sudo()
        try:
            payload = self._matheel_payload()
        except UserError as e:
            self.write({"matheel_state": "error", "matheel_message": str(e)})
            return True
        status, res = company._matheel_request(
            "/invoices", {"device_token": company.matheel_device_token, "pdf": True, "invoice": payload}
        )
        if status in RETRY:
            _logger.info("Matheel: %s retry later (%s)", self.name, status)
            self.matheel_message = _("Will retry: %(status)s %(detail)s", status=status, detail=res)
            return False
        if status != 200:
            self.write({"matheel_state": "error", "matheel_message": self._matheel_explain(status, res)})
            return True

        company.matheel_device_token = res["device_token"]  # the next invoice must use the new token
        self.write(
            {
                "matheel_state": res["status"],
                "matheel_qr": res.get("qr"),
                "matheel_uuid": res.get("zatca_uuid"),
                "matheel_counter": res.get("counter") or 0,
                "matheel_message": "\n".join(self._matheel_warnings(res)) or False,
            }
        )
        self._matheel_attach(res)
        return True

    def _matheel_warnings(self, res):
        """ZATCA's warnings, plus one if the legal total differs from Odoo's (e.g. per-line VAT rounding)."""
        warnings = [str(w) for w in res.get("warnings") or []]
        xml = base64.b64decode(res.get("invoice_xml") or "").decode("utf-8", "ignore")
        payable = re.search(r"PayableAmount[^>]*>([^<]+)<", xml)
        if payable and self.currency_id.compare_amounts(float(payable.group(1)), self.amount_total) != 0:
            warnings.append(
                _(
                    "ZATCA total %(zatca)s differs from Odoo total %(odoo)s: set Accounting → Settings → Rounding method to"
                    " 'Round globally'.",
                    zatca=payable.group(1),
                    odoo=self.amount_total,
                )
            )
        return warnings

    def _matheel_attach(self, res):
        """Keeps ZATCA's XML (the legal invoice) and Matheel's bilingual PDF/A-3 on the invoice."""
        files = [("ZATCA-%s.xml" % self.name, res.get("invoice_xml"), "application/xml")]
        if res.get("pdf"):
            files.append(("ZATCA-%s.pdf" % self.name, res["pdf"], "application/pdf"))
        for name, data, mime in files:
            if data:
                self.env["ir.attachment"].create(
                    {
                        "name": name.replace("/", "-"),
                        "raw": base64.b64decode(data),
                        "res_model": self._name,
                        "res_id": self.id,
                        "mimetype": mime,
                    }
                )
        self.message_post(body=_("ZATCA: %(status)s (counter %(counter)s)", status=res["status"], counter=res.get("counter")))

    @staticmethod
    def _matheel_explain(status, res):
        """One readable line per ZATCA/Matheel error."""
        errors = res.get("errors") or []
        lines = [("%s: %s" % (e.get("code"), e.get("message"))) if isinstance(e, dict) else str(e) for e in errors]
        return "%s %s\n%s" % (status, res.get("error") or "", "\n".join(lines) or res)

    def _matheel_payload(self):
        """The invoice in Matheel's shape: lines after discounts, VAT category per line, buyer for B2B."""
        self.ensure_one()
        partner = self.commercial_partner_id
        standard = bool(partner.vat) and partner.is_company
        lines = [self._matheel_line(line) for line in self.invoice_line_ids.filtered(lambda l: l.display_type == "product")]
        if not lines:
            raise UserError(_("The invoice has no product lines."))
        payload = {
            "type": "standard" if standard else "simplified",
            "document_type": "credit_note" if self.move_type == "out_refund" else "invoice",
            "number": self.name,
            "issued_at": fields.Datetime.now().strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            "currency": self.currency_id.name,
            "line_items": lines,
        }
        if self.move_type == "out_refund":
            original = self.reversed_entry_id.name or self.ref
            if not original:
                raise UserError(_("A credit note needs the original invoice (reverse it from the invoice) | إشعار الدائن يحتاج الفاتورة الأصلية"))
            payload["original_invoice_number"] = original
            payload["note"] = self.ref or _("Refund")
        if standard:
            payload["buyer"] = {
                "name": partner.name,
                "vat_number": partner.vat.strip(),
                "street": partner.street or "",
                "building_number": partner.matheel_building_number or "",
                "district": partner.street2 or "",
                "city": partner.city or "",
                "postal_code": partner.zip or "",
                "country": partner.country_id.code or "SA",
            }
        return payload

    def _matheel_line(self, line):
        """One line: net amount after discount (Odoo's subtotal is always before VAT) and its ZATCA category."""
        if line.price_subtotal < 0:
            raise UserError(_("Negative line '%s': use a discount on the line, not a negative line.", line.name))
        item = {
            "name": (line.name or line.product_id.display_name or "-").split("\n")[0][:250],
            "quantity": str(line.quantity),
            "amount": "%.2f" % line.price_subtotal,
        }
        taxes = line.tax_ids.filtered(lambda t: t.amount_type == "percent")
        tax = taxes[:1]
        if tax and tax.amount == 15 and tax.matheel_tax_category in (False, "S"):
            return item
        if not tax or not tax.matheel_tax_category or tax.matheel_tax_category == "S":
            raise UserError(
                _("Line '%s': set the ZATCA category and exemption code on its tax (Accounting → Taxes).", line.name)
            )
        item["tax_category"] = tax.matheel_tax_category
        if tax.matheel_exemption_code:
            item["tax_exemption_reason_code"] = tax.matheel_exemption_code
        return item

    def _matheel_qr_png(self):
        """The ZATCA QR as a base64 PNG for the printed invoice."""
        self.ensure_one()
        png = self.env["ir.actions.report"].barcode("QR", self.matheel_qr, width=180, height=180)
        return base64.b64encode(png).decode()
