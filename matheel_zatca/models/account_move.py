# Posting a customer invoice or credit note queues it; one cron files each device chain's queue in order, so it never forks.
import base64
import logging
import random
import re
import time
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Statuses that mean ZATCA accepted the invoice; anything filed this way is never sent again.
DONE = ("reported", "cleared")
# Seconds one cron run may spend filing before it hands over to a fresh run (well under limit_time_real with one
# 45 s request in flight), so no worker is killed in the middle of a filing.
BUDGET = 60
# A Saudi VAT number: 15 digits, starting and ending with 3. Only these buyers get a standard (B2B) invoice.
SAUDI_VAT = re.compile(r"3\d{13}3")
# 422 codes ZATCA or Matheel will answer the same way every time: the invoice must be fixed, not retried.
PERMANENT = ("zatca_rejected", "invalid", "missing_fields", "invalid_amount", "validation_failed")


class AccountMove(models.Model):
    _inherit = "account.move"

    matheel_state = fields.Selection(
        [("to_send", "Queued"), ("reported", "Reported"), ("cleared", "Cleared"), ("error", "Rejected")],
        "ZATCA",
        copy=False,
        readonly=True,
        tracking=True,
        index="btree_not_null",
    )
    matheel_message = fields.Text("ZATCA message", copy=False, readonly=True)
    matheel_qr = fields.Char("ZATCA QR", copy=False, readonly=True)
    matheel_uuid = fields.Char("ZATCA UUID", copy=False, readonly=True, help="Generated once at posting, sent on every attempt.")
    matheel_counter = fields.Integer("ZATCA counter (ICV)", copy=False, readonly=True)
    matheel_outcome_unknown = fields.Boolean(
        "ZATCA outcome unknown", copy=False, readonly=True, help="Sent, but no answer yet: only this same invoice may be resent."
    )

    def _post(self, soft=True):
        """Blocks what ZATCA can't take before posting, then queues the posted invoices with a UUID each."""
        for move in self.filtered(lambda m: m._matheel_applies()):
            move._matheel_check()
        posted = super()._post(soft)
        to_queue = posted.filtered(lambda m: m._matheel_applies() and m.matheel_state not in DONE)
        for move in to_queue.filtered(lambda m: not m.matheel_uuid):
            move.matheel_uuid = str(uuid.uuid4())
        if to_queue:
            to_queue.write({"matheel_state": "to_send", "matheel_message": False})
            self.env.ref("matheel_zatca.ir_cron_matheel_send")._trigger()
        return posted

    def _matheel_check(self):
        """Raises before posting: only SAR, credit notes from an invoice, and lines Matheel can file."""
        self.ensure_one()
        if self.currency_id.name != "SAR":
            raise UserError(
                _(
                    "%(name)s is in %(currency)s: ZATCA invoices through Matheel must be in SAR."
                    " | الفاتورة لازم تكون بالريال السعودي",
                    name=self.name or _("This invoice"),
                    currency=self.currency_id.name,
                )
            )
        if self.move_type == "out_refund" and not self.reversed_entry_id:
            raise UserError(_("A credit note needs the original invoice (reverse it from the invoice) | إشعار الدائن يحتاج الفاتورة الأصلية"))
        self._matheel_lines()

    def button_draft(self):
        """An invoice ZATCA accepted is final: correct it with a credit note. A queued one leaves the queue."""
        if self.filtered(lambda m: m.matheel_state in DONE):
            raise UserError(
                _(
                    "ZATCA has accepted this invoice, so it can't be edited. Issue a credit note instead."
                    " | الفاتورة اعتمدتها الهيئة ولا يمكن تعديلها، أصدر إشعار دائن."
                )
            )
        if self.filtered(lambda m: m.matheel_state == "to_send" and m.matheel_outcome_unknown):
            raise UserError(
                _(
                    "This invoice was sent to ZATCA and the answer is still pending; it will be resent as it is."
                    " Wait for the result, then issue a credit note if needed. | الفاتورة اترسلت للهيئة ومستنية الرد."
                )
            )
        self.filtered(lambda m: m.matheel_state in ("to_send", "error")).write({"matheel_state": False})
        return super().button_draft()

    def _matheel_applies(self):
        """A customer invoice or credit note of a company (or its root company) that registered a ZATCA device."""
        self.ensure_one()
        return self.move_type in ("out_invoice", "out_refund") and bool(
            self.company_id.sudo()._matheel_owner().matheel_device_token
        )

    def action_matheel_send(self):
        """Queues the selected invoices again (after fixing a rejection) and runs the queue now."""
        for move in self:
            if move.state != "posted" or not move._matheel_applies():
                raise UserError(_("%s is not a posted customer invoice of a company connected to ZATCA.", move.name))
        self.filtered(lambda m: m.matheel_state not in DONE).write({"matheel_state": "to_send"})
        self.env.ref("matheel_zatca.ir_cron_matheel_send")._trigger()

    @api.model
    def _cron_matheel_send(self, limit=20):
        """Files each device chain's queue oldest first, `limit` per chain per run, for at most BUDGET seconds."""
        deadline = time.monotonic() + BUDGET
        self.env.cr.execute("SELECT DISTINCT company_id FROM account_move WHERE matheel_state = 'to_send' AND state = 'posted'")
        chains = {}  # the company holding the device token -> the companies filing on it (itself and token-less branches)
        for company in self.env["res.company"].sudo().browse([r[0] for r in self.env.cr.fetchall()]):
            owner = company._matheel_owner()
            chains[owner] = chains.get(owner, company.browse()) | company
        order = list(chains.items())
        # ponytail: random chain order so a big backlog can't take every run's budget; fair queueing if chains grow to hundreds.
        random.shuffle(order)
        more = False
        for owner, companies in order:
            if time.monotonic() > deadline:
                more = True
                break
            more |= self._matheel_send_chain(owner, companies, limit, deadline)
        if more:
            self.env.ref("matheel_zatca.ir_cron_matheel_send")._trigger()  # the rest in a fresh run, not this worker

    @api.model
    def _matheel_send_chain(self, owner, companies, limit, deadline):
        """Files one chain's queue in order; returns True when it stopped with work left (time or batch limit)."""
        moves = self.search(
            [("matheel_state", "=", "to_send"), ("state", "=", "posted"), ("company_id", "in", companies.ids)],
            order="id",
            limit=limit,
        )
        for move in moves.filtered(lambda m: not m.matheel_uuid):  # queued before 1.1: a UUID before any send
            move.matheel_uuid = str(uuid.uuid4())
        self.env.cr.commit()
        for move in moves:
            if time.monotonic() > deadline:
                return True
            if not move._matheel_lock(owner):
                continue  # filed, reset or cancelled since the search
            go_on = move._matheel_file(owner)
            self.env.cr.commit()
            if not go_on:
                return False  # retry later or needs attention: keep the order, leave the rest of this chain queued
        # A full batch that moved something: there may be more. A full batch of waiting credit notes must not spin the cron.
        return len(moves) == limit and any(m.matheel_state != "to_send" for m in moves)

    def _matheel_lock(self, owner):
        """Locks the chain (its company row) and this move until the next commit; True if the move is still queued."""
        self.ensure_one()
        owner._matheel_lock()
        self.env.cr.execute("SELECT matheel_state, state FROM account_move WHERE id = %s FOR NO KEY UPDATE", [self.id])
        row = self.env.cr.fetchone()
        self.env.invalidate_all()  # read the token and the move as committed, not as cached before the lock
        return row == ("to_send", "posted")

    def _matheel_file(self, owner, depth=0):
        """Sends one locked, queued invoice; returns False when the chain must stop (retry later or needs attention)."""
        self.ensure_one()
        if self.move_type == "out_refund":
            original = self.reversed_entry_id
            if original.matheel_state == "to_send":
                return True  # stays queued: it is filed after its original
            if original.matheel_state not in DONE:
                return self._matheel_error(
                    _(
                        "The original invoice %s was not filed with ZATCA, so this credit note can't be."
                        " File the original first, then send this again. | الفاتورة الأصلية لم تُرسل للهيئة.",
                        original.name or "-",
                    )
                )
        try:
            payload = self._matheel_payload()
        except UserError as e:
            return self._matheel_error(str(e))
        for _attempt in range(3):
            status, res = owner._matheel_request(
                "/invoices", {"device_token": owner.matheel_device_token, "pdf": True, "invoice": payload}, timeout=45
            )
            error = res.get("error")
            code = error.get("code") if isinstance(error, dict) else error
            if status == 200:
                self._matheel_accept(owner, res)
                return True
            if status == 409 and code == "already_filed":
                self._matheel_already_filed(payload, error)
                return True
            if status == 409 and code == "device_token_reused" and error.get("latest_device_token"):
                # The chain moved on (a lost answer, a restored backup): take its latest token and send this again.
                owner.matheel_device_token = error["latest_device_token"]
                self.env.cr.commit()
                if not self._matheel_lock(owner):
                    return True
                continue
            if status == 409 and code == "previous_filing_unknown" and not depth:
                earlier = self.search(
                    [("matheel_uuid", "=", error.get("uuid")), ("matheel_state", "=", "to_send"), ("id", "!=", self.id)],
                    limit=1,
                )
                if earlier and earlier._matheel_lock(owner):
                    earlier._matheel_file(owner, depth=1)  # ZATCA may have it: settle that one before this one
                    self.env.cr.commit()
                    if not self._matheel_lock(owner):
                        return True
                    continue
            break
        explained = self._matheel_explain(status, res)
        if status in (401, 402) or code == "invalid_device_token":
            # The key, plan or certificate needs a person: keep everything queued and say so on the settings.
            owner.matheel_warning = explained
            self.matheel_message = _("Needs attention, will retry: %s", explained)
            return False
        if status == 422 and code in PERMANENT:
            return self._matheel_error(explained)
        _logger.info("Matheel: %s retry later (%s %s)", self.name, status, code)
        self.write(
            {
                "matheel_message": _("Will retry: %s", explained),
                # Anything that may have reached ZATCA: from now on only this same invoice may be sent for it.
                "matheel_outcome_unknown": self.matheel_outcome_unknown or status == 0 or (status >= 500 and status != 503),
            }
        )
        return False

    def _matheel_error(self, message):
        """Marks the invoice rejected (it needs fixing, retrying won't help); the chain goes on with the next one."""
        self.write({"matheel_state": "error", "matheel_message": message, "matheel_outcome_unknown": False})
        return True

    def _matheel_accept(self, owner, res):
        """Saves ZATCA's acceptance and the next device token in their own commit, then attaches the files."""
        owner.write({"matheel_device_token": res["device_token"], "matheel_warning": False})
        self.write(
            {
                "matheel_state": res["status"],
                "matheel_qr": res.get("qr"),
                "matheel_uuid": res.get("zatca_uuid") or self.matheel_uuid,
                "matheel_counter": res.get("counter") or 0,
                "matheel_message": "\n".join(self._matheel_warnings(res)) or False,
                "matheel_outcome_unknown": False,
            }
        )
        self.env.cr.commit()  # the token must survive whatever happens to the attachments
        try:
            with self.env.cr.savepoint():
                self._matheel_attach(res)
        except Exception:
            _logger.exception("Matheel: %s was filed, but its XML/PDF could not be attached", self.name)

    def _matheel_already_filed(self, payload, error):
        """ZATCA accepted this very document (same UUID) earlier on this device: mark it filed, never send it again."""
        self.write(
            {
                "matheel_state": "cleared" if payload["type"] == "standard" else "reported",
                "matheel_uuid": error.get("zatca_uuid") or self.matheel_uuid,
                "matheel_counter": error.get("counter") or 0,
                "matheel_message": False,
                "matheel_outcome_unknown": False,
            }
        )
        self.message_post(
            body=_(
                "ZATCA had already accepted this invoice (counter %s); it was not filed again. Its legal XML is the one"
                " returned at that first acceptance.",
                error.get("counter"),
            )
        )

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
        error = res.get("error") or ""
        if isinstance(error, dict):
            error = " ".join(str(error[k]) for k in ("code", "message", "detail", "fields") if error.get(k))
        errors = res.get("errors") or []
        lines = [("%s: %s" % (e.get("code"), e.get("message"))) if isinstance(e, dict) else str(e) for e in errors]
        return ("%s %s\n%s" % (status, error or res.get("detail") or "", "\n".join(lines))).strip()

    def _matheel_payload(self):
        """The invoice in Matheel's shape: its UUID, lines after discounts, VAT category per line, buyer for B2B."""
        self.ensure_one()
        partner = self.commercial_partner_id
        vat = re.sub(r"\s", "", partner.vat or "")
        # Standard (B2B, cleared) only for a Saudi VAT-registered buyer; foreign or unregistered buyers get simplified.
        standard = bool(SAUDI_VAT.fullmatch(vat)) and partner.country_id.code in (False, "SA")
        items, subtotal = self._matheel_lines()
        payload = {
            "uuid": self.matheel_uuid,
            "type": "standard" if standard else "simplified",
            "document_type": "credit_note" if self.move_type == "out_refund" else "invoice",
            "number": self.name,
            "currency": self.currency_id.name,
            "line_items": items,
        }
        if subtotal is not None:
            payload["subtotal"] = "%.2f" % subtotal  # the document net: positive lines minus the folded negative ones
        if self.move_type == "out_refund":
            if not self.reversed_entry_id:
                raise UserError(_("A credit note needs the original invoice (reverse it from the invoice) | إشعار الدائن يحتاج الفاتورة الأصلية"))
            payload["original_invoice_number"] = self.reversed_entry_id.name
            payload["note"] = self.ref or _("Refund")
        if standard:
            payload["buyer"] = {
                "name": partner.name,
                "vat_number": vat,
                "street": partner.street or "",
                "building_number": partner.matheel_building_number or "",
                "district": partner.street2 or "",
                "city": partner.city or "",
                "postal_code": partner.zip or "",
                "country": "SA",
            }
        return payload

    def _matheel_lines(self):
        """Positive lines as items, and the document net (or None) when negative lines fold into a document discount."""
        lines = self.invoice_line_ids.filtered(lambda l: l.display_type == "product")
        if not lines:
            raise UserError(_("The invoice has no product lines."))
        categories = {line.id: self._matheel_category(line) for line in lines}
        negative = lines.filtered(lambda l: self.currency_id.compare_amounts(l.price_subtotal, 0) < 0)
        if not negative:
            return [self._matheel_item(line, categories[line.id]) for line in lines], None
        # Matheel files a document discount at standard 15%, so a down payment or global discount line can only be
        # folded in when every line is standard-rated.
        if any(category != "S" for category, _code in categories.values()):
            raise UserError(
                _(
                    "Negative lines (down payments, global discounts) can only be sent to ZATCA when every line is"
                    " standard-rated 15%. Use a discount % on the lines instead. | الخصم يكون على السطور"
                )
            )
        subtotal = sum(lines.mapped("price_subtotal"))
        if self.currency_id.compare_amounts(subtotal, 0) <= 0:
            raise UserError(_("After its negative lines the invoice total must still be above zero."))
        return [self._matheel_item(line, categories[line.id]) for line in lines - negative], subtotal

    @staticmethod
    def _matheel_item(line, category):
        """One line: net amount after discount (Odoo's subtotal is always before VAT) and its ZATCA category."""
        item = {
            "name": (line.name or line.product_id.display_name or "-").split("\n")[0][:250],
            "quantity": str(abs(line.quantity)),
            "amount": "%.2f" % line.price_subtotal,
        }
        if category[0] != "S":
            item["tax_category"] = category[0]
            if category[1]:
                item["tax_exemption_reason_code"] = category[1]
        return item

    @staticmethod
    def _matheel_category(line):
        """(ZATCA category, exemption code) of a line's one VAT tax; tax groups count by their children."""
        taxes = line.tax_ids.mapped(lambda t: t.children_tax_ids if t.amount_type == "group" else t)
        taxes = taxes.filtered(lambda t: t.amount_type == "percent")
        if len(taxes) > 1:
            raise UserError(
                _(
                    "Line '%(line)s' has more than one VAT tax (%(taxes)s): ZATCA takes one VAT rate per line.",
                    line=line.name,
                    taxes=", ".join(taxes.mapped("name")),
                )
            )
        if taxes and taxes.amount == 15 and taxes.matheel_tax_category in (False, "S"):
            return "S", None
        if not taxes or taxes.matheel_tax_category in (False, "S"):
            raise UserError(
                _("Line '%s': set the ZATCA category and exemption code on its tax (Accounting → Taxes).", line.name)
            )
        return taxes.matheel_tax_category, taxes.matheel_exemption_code

    def _matheel_qr_png(self):
        """The ZATCA QR as a base64 PNG for the printed invoice."""
        self.ensure_one()
        png = self.env["ir.actions.report"].barcode("QR", self.matheel_qr, width=180, height=180)
        return base64.b64encode(png).decode()
