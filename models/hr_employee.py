# -*- coding: utf-8 -*-
from odoo import fields, models


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    device_id_num = fields.Char(
        groups='hr.group_hr_user',
    )
