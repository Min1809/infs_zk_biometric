# -*- coding: utf-8 -*-
from odoo import fields, models


class DailyAttendance(models.Model):
    """Extend Daily Attendance analysis view with department and ordering"""
    _inherit = 'daily.attendance'
    _order = 'punching_time desc'

    department_id = fields.Many2one(
        'hr.department',
        related='employee_id.department_id',
        store=False,
        string='Department',
        readonly=True
    )
