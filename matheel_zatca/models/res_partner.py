# The Saudi national-address building number, which ZATCA requires and Odoo Community has no field for.
from odoo import fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    matheel_building_number = fields.Char("Building number", size=4, help="4 digits, from the Saudi national address | رقم المبنى")
