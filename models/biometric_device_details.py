# -*- coding: utf-8 -*-
import datetime
import logging
import pytz
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.addons.base.models.res_partner import _tz_get

_logger = logging.getLogger(__name__)

try:
    from zk import ZK, const
except ImportError:
    _logger.error("Please Install pyzk library.")


def _get_friendly_timezones(self):
    """Return common regional timezones with clear labels at top, followed by all timezones"""
    priority_tz = [
        ('Asia/Bangkok', 'Thailand (UTC+7:00 Bangkok / Indochina)'),
        ('Asia/Yangon', 'Myanmar (UTC+6:30 Yangon)'),
        ('Asia/Jakarta', 'Indonesia (UTC+7:00 Jakarta)'),
        ('Asia/Singapore', 'Singapore / Malaysia (UTC+8:00)'),
        ('Asia/Ho_Chi_Minh', 'Vietnam (UTC+7:00 Ho Chi Minh)'),
        ('Asia/Tokyo', 'Japan (UTC+9:00 Tokyo)'),
        ('Asia/Dubai', 'UAE (UTC+4:00 Dubai)'),
        ('Europe/London', 'UK / GMT (UTC+0 / UTC+1)'),
        ('America/New_York', 'US Eastern (UTC-5 / UTC-4)'),
        ('UTC', 'UTC (Coordinated Universal Time)'),
    ]
    seen = {k for k, _ in priority_tz}
    all_tz = _tz_get(self)
    remaining = [(k, v) for k, v in all_tz if k not in seen]
    return priority_tz + remaining


class BiometricDeviceDetails(models.Model):
    """Extend biometric device model with dedicated device roles and timezone management"""
    _inherit = 'biometric.device.details'

    device_type = fields.Selection([
        ('check_in', 'Check-In Only (Entrance)'),
        ('check_out', 'Check-Out Only (Exit)'),
        ('both', 'Both / Toggle (Default)')
    ], string='Device Role', default='both', required=True,
       help="Specify whether this device is dedicated to entrance check-in (supports multiple break returns without checkout), exit check-out, or both.")

    device_tz = fields.Selection(
        selection=_get_friendly_timezones,
        string='Device Timezone',
        default=lambda self: self.env.user.tz or self.env.company.partner_id.tz or 'Asia/Bangkok',
        required=True,
        help="Timezone of the physical location where this device is installed. Defaults to Thailand (UTC+7:00 Bangkok)."
    )

    def action_set_timezone(self):
        """Override to synchronize device clock to device_tz (safeguard against UTC fallback)"""
        for info in self:
            machine_ip = info.device_ip
            zk_port = info.port_number
            try:
                zk = ZK(machine_ip, port=zk_port, timeout=15,
                        password=0, force_udp=False, ommit_ping=False)
            except NameError:
                raise UserError(_("Pyzk module not Found. Please install it with 'pip3 install pyzk'."))
            conn = None
            try:
                conn = info.device_connect(zk)
                if conn:
                    conn.enable_device()
                    tz_name = info.device_tz or self.env.user.tz or info.company_id.partner_id.tz or 'UTC'
                    local_tz = pytz.timezone(tz_name)
                    user_timezone_time = pytz.utc.localize(fields.Datetime.now()).astimezone(local_tz)
                    conn.set_time(user_timezone_time)
                    return {
                        'type': 'ir.actions.client',
                        'tag': 'display_notification',
                        'params': {
                            'message': _('Successfully set device time to %s (%s).') % (
                                user_timezone_time.strftime('%Y-%m-%d %H:%M:%S'), tz_name),
                            'type': 'success',
                            'sticky': False
                        }
                    }
                else:
                    raise UserError(_("Please Check the Connection"))
            except Exception as error:
                raise ValidationError(f'{error}')
            finally:
                if conn:
                    try:
                        conn.disconnect()
                    except Exception:
                        pass

    def action_download_attendance(self):
        """Override download attendance to handle dedicated Entrance/Exit roles without altering device clock"""
        _logger.info("++++++++++++ INFS ZK Attendance Sync Executed ++++++++++++++++++++++")
        zk_attendance = self.env['zk.machine.attendance'].sudo()
        hr_attendance = self.env['hr.attendance'].sudo()

        for info in self:
            machine_ip = info.device_ip
            zk_port = info.port_number
            try:
                zk = ZK(machine_ip, port=zk_port, timeout=15,
                        password=0, force_udp=False, ommit_ping=False)
            except NameError:
                raise UserError(_("Pyzk module not Found. Please install it with 'pip3 install pyzk'."))

            conn = None
            try:
                conn = info.device_connect(zk)
                if not conn:
                    raise UserError(_('Unable to connect to %s (%s), please check parameters and network connection.') % (info.name, info.device_ip))

                # Use device timezone to interpret punch timestamps correctly (READ-ONLY: do NOT overwrite machine clock during download)
                tz_name = info.device_tz or self.env.user.tz or info.company_id.partner_id.tz or 'UTC'
                local_tz = pytz.timezone(tz_name)

                conn.disable_device()
                user = conn.get_users()
                user_dict = {str(u.user_id): u.name for u in (user or []) if hasattr(u, 'user_id')}
                attendance = conn.get_attendance()

                if attendance:
                    attendance = sorted(attendance, key=lambda a: a.timestamp)
                    valid_att_types = dict(zk_attendance._fields['attendance_type'].selection)
                    valid_punch_types = dict(zk_attendance._fields['punch_type'].selection)

                    for each in attendance:
                        atten_time_naive = each.timestamp
                        local_dt = local_tz.localize(atten_time_naive, is_dst=None)
                        utc_dt = local_dt.astimezone(pytz.utc)
                        utc_naive = utc_dt.replace(tzinfo=None)
                        atten_time_str = fields.Datetime.to_string(utc_naive)
                        punch_local_date = local_dt.date()

                        uid_str = str(each.user_id).strip()
                        if not uid_str:
                            continue

                        emp_name = user_dict.get(uid_str, f"Employee {uid_str}")
                        get_user_id = self.env['hr.employee'].sudo().search(
                            [('device_id_num', '=', uid_str)], limit=1)
                        if not get_user_id:
                            get_user_id = self.env['hr.employee'].sudo().create({
                                'device_id_num': uid_str,
                                'name': emp_name
                            })

                        # 1. Save Raw Biometric Log in zk.machine.attendance
                        duplicate_atten_ids = zk_attendance.search(
                            [('device_id_num', '=', uid_str),
                             ('punching_time', '=', atten_time_str)], limit=1)
                        if not duplicate_atten_ids:
                            att_type_val = str(each.status) if str(each.status) in valid_att_types else '0'
                            punch_type_val = str(each.punch) if str(each.punch) in valid_punch_types else '0'

                            zk_attendance.create({
                                'employee_id': get_user_id.id,
                                'device_id_num': uid_str,
                                'attendance_type': att_type_val,
                                'punch_type': punch_type_val,
                                'punching_time': atten_time_str,
                                'address_id': info.address_id.id
                            })

                        # 2. Sync to hr.attendance based on Device Role
                        open_att = hr_attendance.search([
                            ('employee_id', '=', get_user_id.id),
                            ('check_out', '=', False)
                        ], order='check_in desc', limit=1)

                        if info.device_type == 'check_in':
                            # ENTRANCE DEVICE (Check-In Only)
                            if open_att:
                                check_in_utc = pytz.utc.localize(open_att.check_in)
                                check_in_local_dt = check_in_utc.astimezone(local_tz)
                                check_in_local_date = check_in_local_dt.date()

                                if punch_local_date == check_in_local_date:
                                    # Already checked in today: Door access / break re-entry -> DO NOT checkout!
                                    continue
                                elif punch_local_date > check_in_local_date:
                                    # Unclosed shift from yesterday: Auto-close yesterday and open today's check-in
                                    delta_seconds = (utc_naive - open_att.check_in).total_seconds()
                                    if delta_seconds > 0:
                                        auto_checkout = min(
                                            open_att.check_in + datetime.timedelta(hours=9),
                                            utc_naive - datetime.timedelta(seconds=1)
                                        )
                                        open_att.write({'check_out': auto_checkout})
                                    hr_attendance.create({
                                        'employee_id': get_user_id.id,
                                        'check_in': utc_naive
                                    })
                            else:
                                # No open attendance: Check if employee checked out very recently (< 5 mins ago)
                                last_closed_att = hr_attendance.search([
                                    ('employee_id', '=', get_user_id.id),
                                    ('check_out', '!=', False)
                                ], order='check_out desc', limit=1)

                                is_door_test = False
                                if last_closed_att:
                                    diff_since_checkout = (utc_naive - last_closed_att.check_out).total_seconds()
                                    if 0 <= diff_since_checkout < 300:
                                        is_door_test = True

                                if not is_door_test:
                                    hr_attendance.create({
                                        'employee_id': get_user_id.id,
                                        'check_in': utc_naive
                                    })

                        elif info.device_type == 'check_out':
                            # EXIT DEVICE (Check-Out Only)
                            if open_att:
                                check_in_utc = pytz.utc.localize(open_att.check_in)
                                check_in_local_dt = check_in_utc.astimezone(local_tz)
                                check_in_local_date = check_in_local_dt.date()
                                delta_seconds = (utc_naive - open_att.check_in).total_seconds()

                                if punch_local_date == check_in_local_date:
                                    if delta_seconds >= 60:
                                        open_att.write({'check_out': utc_naive})
                                elif punch_local_date > check_in_local_date:
                                    if delta_seconds > 0:
                                        auto_checkout = min(
                                            open_att.check_in + datetime.timedelta(hours=9),
                                            utc_naive - datetime.timedelta(seconds=1)
                                        )
                                        open_att.write({'check_out': auto_checkout})
                            else:
                                # No active check-in: Check if employee checked out earlier today and is tapping exit again
                                last_closed_att = hr_attendance.search([
                                    ('employee_id', '=', get_user_id.id),
                                    ('check_out', '!=', False)
                                ], order='check_out desc', limit=1)

                                if last_closed_att:
                                    last_out_utc = pytz.utc.localize(last_closed_att.check_out)
                                    last_out_local_date = last_out_utc.astimezone(local_tz).date()
                                    if punch_local_date == last_out_local_date and utc_naive > last_closed_att.check_out:
                                        # Update departure to latest exit punch
                                        last_closed_att.write({'check_out': utc_naive})

                        else:
                            # BOTH / TOGGLE (Standard alternating behavior)
                            if open_att:
                                check_in_utc = pytz.utc.localize(open_att.check_in)
                                check_in_local_dt = check_in_utc.astimezone(local_tz)
                                check_in_local_date = check_in_local_dt.date()
                                delta_seconds = (utc_naive - open_att.check_in).total_seconds()

                                if punch_local_date == check_in_local_date:
                                    if delta_seconds >= 120:
                                        open_att.write({'check_out': utc_naive})
                                elif punch_local_date > check_in_local_date:
                                    if delta_seconds > 0:
                                        auto_checkout = min(
                                            open_att.check_in + datetime.timedelta(hours=9),
                                            utc_naive - datetime.timedelta(seconds=1)
                                        )
                                        open_att.write({'check_out': auto_checkout})
                                    hr_attendance.create({
                                        'employee_id': get_user_id.id,
                                        'check_in': utc_naive
                                    })
                            else:
                                last_closed_att = hr_attendance.search([
                                    ('employee_id', '=', get_user_id.id),
                                    ('check_out', '!=', False)
                                ], order='check_out desc', limit=1)

                                is_door_test = False
                                if last_closed_att:
                                    diff_since_checkout = (utc_naive - last_closed_att.check_out).total_seconds()
                                    if 0 <= diff_since_checkout < 300:
                                        is_door_test = True

                                if not is_door_test:
                                    hr_attendance.create({
                                        'employee_id': get_user_id.id,
                                        'check_in': utc_naive
                                    })

                    return {
                        'type': 'ir.actions.client',
                        'tag': 'display_notification',
                        'params': {
                            'message': _('Successfully processed %d attendance record(s) on %s.') % (len(attendance), info.name),
                            'type': 'success',
                            'sticky': False
                        }
                    }
                else:
                    _logger.info("No attendance records found on device: %s (%s)", info.name, info.device_ip)
                    return {
                        'type': 'ir.actions.client',
                        'tag': 'display_notification',
                        'params': {
                            'message': _('No attendance records found on the device.'),
                            'type': 'warning',
                            'sticky': False
                        }
                    }
            finally:
                if conn:
                    try:
                        conn.enable_device()
                    except Exception:
                        pass
                    try:
                        conn.disconnect()
                    except Exception:
                        pass
