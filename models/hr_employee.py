# -*- coding: utf-8 -*-
import logging
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

try:
    from zk import ZK, const
except ImportError:
    _logger.error("Please Install pyzk library.")


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    device_id_num = fields.Char(
        groups='hr.group_hr_user',
    )
    biometric_user_ids = fields.One2many(
        'biometric.device.user',
        'employee_id',
        string='Biometric Device Users',
        groups='hr.group_hr_user',
    )
    is_synced_to_biometric = fields.Boolean(
        string='Synced to Biometric Device',
        compute='_compute_is_synced_to_biometric',
        groups='hr.group_hr_user',
    )

    @api.depends('biometric_user_ids', 'device_id_num')
    def _compute_is_synced_to_biometric(self):
        for emp in self:
            emp.is_synced_to_biometric = bool(emp.biometric_user_ids)

    def write(self, vals):
        res = super().write(vals)
        if 'device_id_num' in vals:
            DeviceUser = self.env['biometric.device.user'].sudo()
            for emp in self:
                uid_clean = (emp.device_id_num or '').strip()
                # Unlink old device users that no longer match
                old_links = DeviceUser.search([
                    ('employee_id', '=', emp.id),
                    ('user_id', '!=', uid_clean),
                ])
                if old_links:
                    old_links.write({'employee_id': False})
                # Link matching device users
                if uid_clean:
                    matching = DeviceUser.search([('user_id', '=', uid_clean)])
                    if matching:
                        matching.write({'employee_id': emp.id})
        return res

    def _get_next_available_device_id(self, device_users_by_device):
        """Calculate the next available numeric Biometric Device ID across Odoo and connected devices"""
        max_id = 0

        # 1. Check all existing hr.employee device_id_num in Odoo
        employees = self.env['hr.employee'].sudo().with_context(active_test=False).search([
            ('device_id_num', '!=', False)
        ])
        for emp in employees:
            val = (emp.device_id_num or '').strip()
            if val.isdigit():
                max_id = max(max_id, int(val))

        # 2. Check all existing biometric.device.user records in Odoo
        dev_users = self.env['biometric.device.user'].sudo().search([])
        for du in dev_users:
            val = (du.user_id or '').strip()
            if val.isdigit():
                max_id = max(max_id, int(val))

        # 3. Check all live users fetched from connected physical devices
        for users in device_users_by_device.values():
            for u in users:
                val = str(getattr(u, 'user_id', '') or '').strip()
                if val.isdigit():
                    max_id = max(max_id, int(val))

        return str(max_id + 1)

    def action_create_user_on_device(self):
        """Create or update this employee on all configured ZKTeco Biometric Devices and link device_id_num"""
        self.ensure_one()

        devices = self.env['biometric.device.details'].sudo().search([])
        if not devices:
            raise UserError(_("No Biometric Devices are configured in Odoo. Please configure a device under Attendances -> Biometric Device first."))

        uid_clean = (self.device_id_num or '').strip()
        if uid_clean:
            # Ensure no other active employee already uses the same Biometric Device ID
            duplicate_emp = self.env['hr.employee'].sudo().search([
                ('id', '!=', self.id),
                ('device_id_num', '=', uid_clean),
            ], limit=1)
            if duplicate_emp:
                raise ValidationError(_(
                    "Biometric Device ID '%s' is already assigned to another employee (%s). "
                    "Please clear it to auto-generate a new ID or enter a unique ID."
                ) % (uid_clean, duplicate_emp.name))

        # Connect to devices and gather existing device users first
        device_users_by_device = {}
        connection_errors = []

        for device in devices:
            try:
                zk = ZK(device.device_ip, port=device.port_number, timeout=15,
                        password=0, force_udp=False, ommit_ping=False)
            except NameError:
                raise UserError(_("Pyzk module not found. Please install it with 'pip3 install pyzk'."))

            conn = None
            try:
                conn = device.device_connect(zk)
                if not conn:
                    connection_errors.append(f"{device.name} ({device.device_ip}): Unable to connect")
                    continue
                conn.disable_device()
                device_users_by_device[device.id] = conn.get_users() or []
            except Exception as err:
                connection_errors.append(f"{device.name} ({device.device_ip}): {err}")
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

        if not device_users_by_device:
            raise UserError(_(
                "Could not connect to any configured Biometric Device:\n%s"
            ) % "\n".join(connection_errors))

        # Auto-generate next available numeric ID if device_id_num is empty
        if not uid_clean:
            uid_clean = self._get_next_available_device_id(device_users_by_device)
            self.sudo().write({'device_id_num': uid_clean})

        # ZKTeco supports up to 24 characters for user name
        safe_name = (self.name or f"Employee {uid_clean}").strip()[:24]
        DeviceUser = self.env['biometric.device.user'].sudo()
        synced_device_names = []

        for device in devices:
            if device.id not in device_users_by_device:
                continue

            conn = None
            try:
                zk = ZK(device.device_ip, port=device.port_number, timeout=15,
                        password=0, force_udp=False, ommit_ping=False)
                conn = device.device_connect(zk)
                if not conn:
                    connection_errors.append(f"{device.name}: Connection lost during user creation")
                    continue

                conn.disable_device()
                existing_users = conn.get_users() or []
                matched_user = next(
                    (u for u in existing_users if str(getattr(u, 'user_id', '')).strip() == uid_clean),
                    None
                )

                if matched_user:
                    target_uid = matched_user.uid
                    priv_int = int(getattr(matched_user, 'privilege', 0) or 0)
                    pwd = getattr(matched_user, 'password', '') or ''
                    grp = str(getattr(matched_user, 'group_id', '') or '')
                    card_val = int(getattr(matched_user, 'card', 0) or 0)
                else:
                    max_uid = max([getattr(u, 'uid', 0) or 0 for u in existing_users], default=0)
                    target_uid = max_uid + 1
                    priv_int = const.USER_DEFAULT
                    pwd = ''
                    grp = ''
                    card_val = 0

                conn.set_user(
                    uid=target_uid,
                    name=safe_name,
                    privilege=priv_int,
                    password=pwd,
                    group_id=grp,
                    user_id=uid_clean,
                    card=card_val,
                )
                conn.refresh_data()

                # Create or update local biometric.device.user record
                priv_str = str(priv_int) if str(priv_int) in {'0', '2', '6', '14'} else '0'
                dev_user_rec = DeviceUser.search([
                    ('device_id', '=', device.id),
                    ('user_id', '=', uid_clean),
                ], limit=1)

                vals = {
                    'device_id': device.id,
                    'uid': target_uid,
                    'user_id': uid_clean,
                    'name': safe_name,
                    'privilege': priv_str,
                    'card': str(card_val) if card_val else '',
                    'group_id': grp,
                    'employee_id': self.id,
                    'last_sync_date': fields.Datetime.now(),
                }
                if dev_user_rec:
                    dev_user_rec.write(vals)
                else:
                    DeviceUser.create(vals)

                synced_device_names.append(device.name)
            except Exception as err:
                connection_errors.append(f"{device.name}: {err}")
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

        if not synced_device_names:
            raise UserError(_("Failed to create user on biometric device(s):\n%s") % "\n".join(connection_errors))

        msg = _("Employee '%s' (Biometric ID: %s) created/synced on: %s.") % (
            safe_name, uid_clean, ", ".join(synced_device_names)
        )
        if connection_errors:
            msg += _("\nWarnings: %s") % "; ".join(connection_errors)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Synced to Biometric Device'),
                'message': msg,
                'type': 'warning' if connection_errors else 'success',
                'sticky': bool(connection_errors),
                'next': {'type': 'ir.actions.client', 'tag': 'reload'},
            }
        }
