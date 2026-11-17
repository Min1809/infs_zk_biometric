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

    device_user_ids = fields.One2many(
        'biometric.device.user',
        'device_id',
        string='Device Users',
    )
    device_user_count = fields.Integer(
        string='Device Users Count',
        compute='_compute_device_user_count',
    )

    def _compute_device_user_count(self):
        for rec in self:
            rec.device_user_count = len(rec.device_user_ids)

    def _sync_users_from_device(self):
        """Connect to this device, fetch users via conn.get_users(), and sync to biometric.device.user"""
        self.ensure_one()
        try:
            zk = ZK(self.device_ip, port=self.port_number, timeout=15,
                    password=0, force_udp=False, ommit_ping=False)
        except NameError:
            raise UserError(_("Pyzk module not Found. Please install it with 'pip3 install pyzk'."))

        conn = None
        try:
            conn = self.device_connect(zk)
            if not conn:
                raise UserError(_('Unable to connect to %s (%s), please check parameters and network connection.') % (self.name, self.device_ip))

            conn.disable_device()
            zk_users = conn.get_users() or []
            try:
                zk_templates = conn.get_templates() or []
            except Exception as tpl_err:
                _logger.warning("Could not fetch fingerprint templates from %s: %s", self.name, tpl_err)
                zk_templates = []

            fp_count_by_uid = {}
            for tpl in zk_templates:
                if getattr(tpl, 'valid', 1):
                    t_uid = getattr(tpl, 'uid', 0)
                    if t_uid:
                        fp_count_by_uid[t_uid] = fp_count_by_uid.get(t_uid, 0) + 1

            DeviceUser = self.env['biometric.device.user'].sudo()
            Employee = self.env['hr.employee'].sudo()

            existing_records = DeviceUser.search([('device_id', '=', self.id)])
            existing_by_uid = {r.user_id: r for r in existing_records if r.user_id}

            employees = Employee.search([('device_id_num', '!=', False)])
            emp_by_dev_id = {emp.device_id_num.strip(): emp.id for emp in employees if emp.device_id_num and emp.device_id_num.strip()}

            valid_privs = {'0', '2', '6', '14'}
            now_dt = fields.Datetime.now()
            synced_user_ids = set()

            for u in zk_users:
                uid_str = str(getattr(u, 'user_id', '') or '').strip()
                if not uid_str:
                    continue
                synced_user_ids.add(uid_str)

                priv_str = str(getattr(u, 'privilege', 0) or 0)
                if priv_str not in valid_privs:
                    priv_str = '14' if priv_str != '0' else '0'

                card_val = getattr(u, 'card', 0)
                card_str = str(card_val) if card_val else ''
                u_name = (getattr(u, 'name', '') or '').strip() or f"User {uid_str}"
                internal_uid = getattr(u, 'uid', 0) or 0

                vals = {
                    'device_id': self.id,
                    'uid': internal_uid,
                    'user_id': uid_str,
                    'name': u_name,
                    'privilege': priv_str,
                    'fingerprint_count': fp_count_by_uid.get(internal_uid, 0),
                    'card': card_str,
                    'group_id': str(getattr(u, 'group_id', '') or ''),
                    'employee_id': emp_by_dev_id.get(uid_str, False),
                    'last_sync_date': now_dt,
                }

                if uid_str in existing_by_uid:
                    existing_by_uid[uid_str].write(vals)
                else:
                    DeviceUser.create(vals)

            # Remove stale user records that no longer exist on the physical device
            stale_records = existing_records.filtered(lambda r: r.user_id not in synced_user_ids)
            if stale_records:
                stale_records.unlink()

            return len(synced_user_ids)
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

    def _replicate_fingerprints_to_devices(self, target_devices, only_user_ids=None, preloaded_fingers_by_userid=None):
        """Read users & fingerprint templates from self (source device) and replicate to target_devices"""
        self.ensure_one()
        errors = []
        try:
            src_zk = ZK(self.device_ip, port=self.port_number, timeout=30,
                        password=0, force_udp=False, ommit_ping=True)
        except NameError:
            raise UserError(_("Pyzk module not Found. Please install it with 'pip3 install pyzk'."))

        src_conn = None
        src_users = []
        src_templates = []
        fallback_fingers_by_uid = {}
        try:
            src_conn = self.device_connect(src_zk)
            if not src_conn:
                raise UserError(_("Unable to connect to source device %s (%s).") % (self.name, self.device_ip))
            src_conn.disable_device()
            src_users = src_conn.get_users() or []
            try:
                src_templates = src_conn.get_templates() or []
            except Exception as tpl_err:
                _logger.warning("get_templates() failed on %s: %s", self.name, tpl_err)
                src_templates = []

            # If syncing specific user(s) and bulk get_templates returned none for them, read per-finger (0..9)
            if only_user_ids:
                tpl_uids = {getattr(t, 'uid', 0) for t in src_templates if getattr(t, 'valid', 1)}
                for u in src_users:
                    u_id_str = str(getattr(u, 'user_id', '') or '').strip()
                    if u_id_str in only_user_ids and u.uid not in tpl_uids:
                        if preloaded_fingers_by_userid and u_id_str in preloaded_fingers_by_userid:
                            fallback_fingers_by_uid[u.uid] = preloaded_fingers_by_userid[u_id_str]
                        else:
                            u_fingers = []
                            for fid in range(10):
                                try:
                                    fp = src_conn.get_user_template(uid=u.uid, temp_id=fid)
                                    if fp and getattr(fp, 'template', None) and len(fp.template) > 0:
                                        u_fingers.append(fp)
                                except Exception:
                                    continue
                            if u_fingers:
                                fallback_fingers_by_uid[u.uid] = u_fingers
        finally:
            if src_conn:
                try:
                    src_conn.enable_device()
                except Exception:
                    pass
                try:
                    src_conn.disconnect()
                except Exception:
                    pass

        # Map source templates by internal uid -> list of Finger objects
        templates_by_src_uid = dict(fallback_fingers_by_uid)
        for tpl in src_templates:
            if getattr(tpl, 'valid', 1):
                templates_by_src_uid.setdefault(tpl.uid, []).append(tpl)

        # Filter source users
        users_to_sync = []
        for u in src_users:
            uid_str = str(getattr(u, 'user_id', '') or '').strip()
            if not uid_str:
                continue
            if only_user_ids and uid_str not in only_user_ids:
                continue
            users_to_sync.append(u)

        # Update source device records in Odoo
        try:
            self._sync_users_from_device()
        except Exception:
            pass

        users_synced_set = set()
        total_fp_copied = 0
        DeviceUser = self.env['biometric.device.user'].sudo()
        Employee = self.env['hr.employee'].sudo()
        employees = Employee.search([('device_id_num', '!=', False)])
        emp_by_dev_id = {emp.device_id_num.strip(): emp.id for emp in employees if emp.device_id_num and emp.device_id_num.strip()}
        valid_privs = {'0', '2', '6', '14'}

        for dest_dev in target_devices:
            if dest_dev.id == self.id:
                continue
            dest_conn = None
            try:
                dest_zk = ZK(dest_dev.device_ip, port=dest_dev.port_number, timeout=15,
                             password=0, force_udp=False, ommit_ping=False)
                dest_conn = dest_dev.device_connect(dest_zk)
                if not dest_conn:
                    errors.append(f"{dest_dev.name} ({dest_dev.device_ip}): Unable to connect")
                    continue

                dest_conn.disable_device()
                dest_users = dest_conn.get_users() or []
                dest_user_by_userid = {str(getattr(du, 'user_id', '')).strip(): du for du in dest_users if getattr(du, 'user_id', '')}
                max_dest_uid = max([getattr(du, 'uid', 0) or 0 for du in dest_users], default=0)

                for src_u in users_to_sync:
                    uid_str = str(src_u.user_id).strip()
                    src_fingers = templates_by_src_uid.get(src_u.uid, [])
                    # If single-user sync and bulk get_templates didn't return, fallback to get_user_template per finger
                    dest_u = dest_user_by_userid.get(uid_str)

                    safe_name = (getattr(src_u, 'name', '') or f"User {uid_str}").strip()[:24]
                    priv_int = int(getattr(src_u, 'privilege', 0) or 0)
                    pwd = getattr(src_u, 'password', '') or ''
                    grp = str(getattr(src_u, 'group_id', '') or '')
                    card_val = int(getattr(src_u, 'card', 0) or 0)

                    if dest_u:
                        target_uid = dest_u.uid
                    else:
                        max_dest_uid += 1
                        target_uid = max_dest_uid

                    # Ensure user exists / is updated on destination device
                    dest_conn.set_user(
                        uid=target_uid,
                        name=safe_name,
                        privilege=priv_int,
                        password=pwd,
                        group_id=grp,
                        user_id=uid_str,
                        card=card_val,
                    )

                    # Re-fetch or construct User object with target_uid for save_user_template
                    if src_fingers:
                        from zk.user import User
                        from zk.finger import Finger
                        target_user_obj = User(
                            target_uid,
                            safe_name,
                            priv_int,
                            pwd,
                            grp,
                            uid_str,
                            card_val,
                        )
                        mapped_fingers = [
                            Finger(target_uid, f.fid, f.valid, f.template)
                            for f in src_fingers
                        ]
                        dest_conn.save_user_template(target_user_obj, mapped_fingers)
                        total_fp_copied += len(mapped_fingers)
                        users_synced_set.add(uid_str)
                    else:
                        users_synced_set.add(uid_str)

                    # Sync Odoo biometric.device.user record for destination device
                    priv_str = str(priv_int) if str(priv_int) in valid_privs else '0'
                    dev_rec = DeviceUser.search([
                        ('device_id', '=', dest_dev.id),
                        ('user_id', '=', uid_str),
                    ], limit=1)
                    vals = {
                        'device_id': dest_dev.id,
                        'uid': target_uid,
                        'user_id': uid_str,
                        'name': safe_name,
                        'privilege': priv_str,
                        'fingerprint_count': len(src_fingers),
                        'card': str(card_val) if card_val else '',
                        'group_id': grp,
                        'employee_id': emp_by_dev_id.get(uid_str, False),
                        'last_sync_date': fields.Datetime.now(),
                    }
                    if dev_rec:
                        dev_rec.write(vals)
                    else:
                        DeviceUser.create(vals)

                dest_conn.refresh_data()
            except Exception as err:
                errors.append(f"{dest_dev.name}: {err}")
            finally:
                if dest_conn:
                    try:
                        dest_conn.enable_device()
                    except Exception:
                        pass
                    try:
                        dest_conn.disconnect()
                    except Exception:
                        pass

        return len(users_synced_set), total_fp_copied, errors

    def action_sync_fingerprints_to_other_devices(self):
        """Button on Biometric Device form to push all users & fingerprints from this device to other devices"""
        self.ensure_one()
        other_devices = self.env['biometric.device.details'].sudo().search([('id', '!=', self.id)])
        if not other_devices:
            raise UserError(_("No other Biometric Devices configured to sync fingerprints to."))

        u_cnt, fp_cnt, errors = self._replicate_fingerprints_to_devices(other_devices)
        if errors and u_cnt == 0 and fp_cnt == 0:
            raise UserError(_("Failed to sync fingerprints from %s:\n%s") % (self.name, "\n".join(errors)))

        msg = _("Synced %d fingerprint template(s) across %d user(s) from %s to %s.") % (
            fp_cnt, u_cnt, self.name, ", ".join(other_devices.mapped('name'))
        )
        if errors:
            msg += _("\nWarnings: %s") % "; ".join(errors)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Fingerprints Replicated'),
                'message': msg,
                'type': 'warning' if errors else 'success',
                'sticky': bool(errors),
            }
        }

    def action_sync_device_users(self):
        """Button action on Biometric Device form to sync users from this device"""
        self.ensure_one()
        count = self._sync_users_from_device()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Users Synced'),
                'message': _('Successfully synced %d user(s) from %s.') % (count, self.name),
                'type': 'success',
                'sticky': False,
            }
        }

    def action_view_device_users(self):
        """Smart button action to view users on this biometric device"""
        self.ensure_one()
        action = self.env['ir.actions.actions']._for_xml_id('infs_zk_biometric.biometric_device_user_action')
        action['domain'] = [('device_id', '=', self.id)]
        action['context'] = {'default_device_id': self.id}
        return action

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
                    new_punches_count = 0

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

                        # Also keep biometric.device.user in sync if not yet present
                        dev_user = self.env['biometric.device.user'].sudo().search([
                            ('device_id', '=', info.id),
                            ('user_id', '=', uid_str),
                        ], limit=1)
                        if not dev_user:
                            self.env['biometric.device.user'].sudo().create({
                                'device_id': info.id,
                                'user_id': uid_str,
                                'name': emp_name,
                                'employee_id': get_user_id.id,
                            })
                        elif not dev_user.employee_id:
                            dev_user.write({'employee_id': get_user_id.id})

                        # 1. Save Raw Biometric Log in zk.machine.attendance
                        duplicate_atten_ids = zk_attendance.search(
                            [('device_id_num', '=', uid_str),
                             ('punching_time', '=', atten_time_str)], limit=1)
                        if duplicate_atten_ids:
                            # Already downloaded and synced in a previous fetch -> Skip to prevent re-processing historical logs against today's state
                            continue

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
                        new_punches_count += 1

                        # 2. Sync to hr.attendance based on Device Role
                        try:
                            # Check if punch falls inside an existing closed attendance to prevent overlapping errors
                            overlapping = hr_attendance.search([
                                ('employee_id', '=', get_user_id.id),
                                ('check_in', '<=', utc_naive),
                                ('check_out', '>=', utc_naive),
                            ], limit=1)
                            if overlapping:
                                continue

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
                                                open_att.check_in + datetime.timedelta(hours=8),
                                                utc_naive - datetime.timedelta(seconds=1)
                                            )
                                            open_att.write({'check_out': auto_checkout})
                                        hr_attendance.create({
                                            'employee_id': get_user_id.id,
                                            'check_in': utc_naive
                                        })
                                else:
                                    # No open attendance: Check if employee already completed shift today or checked out recently
                                    last_closed_att = hr_attendance.search([
                                        ('employee_id', '=', get_user_id.id),
                                        ('check_out', '!=', False)
                                    ], order='check_out desc', limit=1)

                                    if last_closed_att:
                                        last_out_utc = pytz.utc.localize(last_closed_att.check_out)
                                        last_out_local_date = last_out_utc.astimezone(local_tz).date()
                                        if punch_local_date == last_out_local_date:
                                            # Already completed shift today: Evening entrance tap is door access re-entry
                                            continue

                                        diff_since_checkout = (utc_naive - last_closed_att.check_out).total_seconds()
                                        if 0 <= diff_since_checkout < 300:
                                            continue

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
                                                open_att.check_in + datetime.timedelta(hours=8),
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
                                                open_att.check_in + datetime.timedelta(hours=8),
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
                        except Exception as punch_err:
                            _logger.warning("Could not sync punch for %s at %s: %s", get_user_id.name, atten_time_str, punch_err)

                    return {
                        'type': 'ir.actions.client',
                        'tag': 'display_notification',
                        'params': {
                            'message': _('Successfully processed %d record(s) (%d new) on %s.') % (len(attendance), new_punches_count, info.name),
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
