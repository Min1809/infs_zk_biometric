# -*- coding: utf-8 -*-
import logging
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

try:
    from zk import ZK, const
except ImportError:
    _logger.error("Please Install pyzk library.")


class BiometricDeviceUser(models.Model):
    """Model representing users stored on ZKTeco Biometric Devices"""
    _name = 'biometric.device.user'
    _description = 'Biometric Device User'
    _order = 'device_id, uid asc, user_id asc'

    device_id = fields.Many2one(
        'biometric.device.details',
        string='Biometric Device',
        required=True,
        ondelete='cascade',
        index=True,
    )
    uid = fields.Integer(
        string='Device UID',
        help='Internal index (UID) assigned by the ZKTeco device.',
    )
    user_id = fields.Char(
        string='Biometric Device ID',
        required=True,
        index=True,
        help='User ID on the biometric device (matches Employee Biometric Device ID / device_id_num).',
    )
    name = fields.Char(
        string='Name on Device',
        required=True,
        help='User name stored on the biometric device (max 24 characters on ZKTeco).',
    )
    employee_id = fields.Many2one(
        'hr.employee',
        string='Linked Employee',
        compute='_compute_employee_id',
        inverse='_inverse_employee_id',
        store=True,
        ondelete='set null',
        help='Odoo Employee linked via Biometric Device ID (device_id_num).',
    )
    department_id = fields.Many2one(
        'hr.department',
        related='employee_id.department_id',
        store=True,
        string='Department',
        readonly=True,
    )
    privilege = fields.Selection([
        ('0', 'User'),
        ('2', 'Enroller'),
        ('6', 'Manager'),
        ('14', 'Super Admin'),
    ], string='Privilege', default='0', required=True)
    fingerprint_count = fields.Integer(
        string='Fingerprints',
        default=0,
        readonly=True,
        help='Number of enrolled fingerprint templates stored for this user on this device.',
    )
    card = fields.Char(
        string='Card Number',
        help='RFID Card number stored on the device.',
    )
    group_id = fields.Char(
        string='Group ID',
        help='Group ID on the biometric device.',
    )
    last_sync_date = fields.Datetime(
        string='Last Synced',
        default=fields.Datetime.now,
        readonly=True,
    )

    _sql_constraints = [
        (
            'device_user_id_uniq',
            'unique(device_id, user_id)',
            'Biometric Device ID must be unique per Biometric Device!'
        ),
    ]

    @api.depends('name', 'user_id', 'device_id.name', 'fingerprint_count')
    def _compute_display_name(self):
        for rec in self:
            dev_name = rec.device_id.name or ''
            uid_str = rec.user_id or ''
            fp_info = f", {rec.fingerprint_count} FP" if rec.fingerprint_count else ""
            if dev_name and uid_str:
                rec.display_name = f"{dev_name} (ID: {uid_str}{fp_info})"
            else:
                rec.display_name = rec.name or uid_str or _('Device User')

    @api.depends('user_id')
    def _compute_employee_id(self):
        """Automatically match device user to hr.employee by device_id_num"""
        user_ids = [r.user_id.strip() for r in self if r.user_id and r.user_id.strip()]
        emp_map = {}
        if user_ids:
            employees = self.env['hr.employee'].sudo().with_context(active_test=False).search([
                ('device_id_num', 'in', user_ids)
            ])
            for emp in sorted(employees, key=lambda e: 0 if e.active else 1):
                if emp.device_id_num and emp.device_id_num.strip() not in emp_map:
                    emp_map[emp.device_id_num.strip()] = emp.id

        for rec in self:
            uid_clean = rec.user_id.strip() if rec.user_id else ''
            rec.employee_id = emp_map.get(uid_clean, False)

    def _inverse_employee_id(self):
        """When manually linking an employee on Device User, set employee's device_id_num"""
        for rec in self:
            if rec.employee_id and rec.user_id:
                uid_clean = rec.user_id.strip()
                if rec.employee_id.device_id_num != uid_clean:
                    rec.employee_id.sudo().with_context(skip_biometric_device_id_push=True).write({
                        'device_id_num': uid_clean
                    })

    def action_sync_all_devices(self):
        """Connect to all configured biometric devices and sync their user lists and fingerprint counts"""
        devices = self.env['biometric.device.details'].search([])
        if not devices:
            raise UserError(_("No Biometric Devices configured. Please configure a device first."))

        total_synced = 0
        errors = []
        for device in devices:
            try:
                count = device._sync_users_from_device()
                total_synced += count
            except Exception as err:
                errors.append(f"{device.name} ({device.device_ip}): {err}")

        if errors and total_synced == 0:
            raise UserError(_("Failed to sync users from devices:\n%s") % "\n".join(errors))

        msg = _("Successfully synced %d user record(s) across %d device(s).") % (total_synced, len(devices) - len(errors))
        if errors:
            msg += _("\nWarnings: %s") % "; ".join(errors)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Device Users Synced'),
                'message': msg,
                'type': 'warning' if errors else 'success',
                'sticky': bool(errors),
                'next': {'type': 'ir.actions.client', 'tag': 'reload'},
            }
        }

    def action_sync_all_fingerprints(self):
        """Bulk sync all users and their enrolled fingerprint templates from Entrance device(s) to Exit/other device(s)"""
        devices = self.env['biometric.device.details'].sudo().search([])
        if len(devices) < 2:
            raise UserError(_("At least 2 Biometric Devices (e.g. Entrance and Exit) are required to sync fingerprints between devices."))

        # Prioritize Check-In (Entrance) devices as source; fallback to first device if none explicitly set to check_in
        source_devices = devices.filtered(lambda d: d.device_type == 'check_in')
        if not source_devices:
            source_devices = devices[:1]

        target_devices = devices - source_devices
        if not target_devices:
            target_devices = devices

        total_users_synced = 0
        total_fp_synced = 0
        errors = []

        for src_dev in source_devices:
            dest_devs = [d for d in target_devices if d.id != src_dev.id]
            if not dest_devs:
                continue
            try:
                u_cnt, fp_cnt, dev_errs = src_dev._replicate_fingerprints_to_devices(dest_devs)
                total_users_synced += u_cnt
                total_fp_synced += fp_cnt
                errors.extend(dev_errs)
            except Exception as err:
                errors.append(f"{src_dev.name}: {err}")

        if errors and total_fp_synced == 0 and total_users_synced == 0:
            raise UserError(_("Failed to sync fingerprints:\n%s") % "\n".join(errors))

        msg = _("Synced %d fingerprint template(s) for %d user(s) from %s to %s.") % (
            total_fp_synced,
            total_users_synced,
            ", ".join(source_devices.mapped('name')),
            ", ".join(target_devices.mapped('name')),
        )
        if errors:
            msg += _("\nWarnings: %s") % "; ".join(errors)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Fingerprints Synced'),
                'message': msg,
                'type': 'warning' if errors else 'success',
                'sticky': bool(errors),
                'next': {'type': 'ir.actions.client', 'tag': 'reload'},
            }
        }

    def action_sync_user_fingerprints(self):
        """Sync this specific user's fingerprint templates across all configured biometric devices"""
        self.ensure_one()
        uid_clean = (self.user_id or '').strip()
        if not uid_clean:
            raise UserError(_("This device user does not have a valid Biometric Device ID."))

        if self.employee_id:
            return self.employee_id.action_sync_employee_fingerprints()

        # If not linked to an employee yet, sync by user_id directly from this user's device (or entrance device) to other devices
        devices = self.env['biometric.device.details'].sudo().search([])
        other_devices = devices.filtered(lambda d: d.id != self.device_id.id)
        if not other_devices:
            raise UserError(_("No other Biometric Devices configured to sync fingerprints to."))

        u_cnt, fp_cnt, errs = self.device_id._replicate_fingerprints_to_devices(
            other_devices, only_user_ids={uid_clean}
        )
        if errs and fp_cnt == 0:
            raise UserError(_("Failed to sync fingerprints for ID %s:\n%s") % (uid_clean, "\n".join(errs)))

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('User Fingerprints Synced'),
                'message': _("Synced %d fingerprint(s) for %s (ID: %s) to %s.") % (
                    fp_cnt, self.name, uid_clean, ", ".join(other_devices.mapped('name'))
                ),
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.client', 'tag': 'reload'},
            }
        }

    def action_create_or_link_employee(self):
        """Link to existing employee by name/ID or create a new Odoo Employee from this Device User"""
        self.ensure_one()
        uid_clean = (self.user_id or '').strip()
        if not uid_clean:
            raise UserError(_("This device user does not have a valid Biometric Device ID."))

        Employee = self.env['hr.employee'].sudo()
        emp = Employee.with_context(active_test=False).search([('device_id_num', '=', uid_clean)], limit=1)
        if not emp and self.name:
            emp = Employee.with_context(active_test=False).search([('name', '=ilike', self.name.strip())], limit=1)
            if emp and not emp.device_id_num:
                emp.with_context(skip_biometric_device_id_push=True).write({'device_id_num': uid_clean})
            else:
                emp = False

        if not emp:
            emp = Employee.with_context(skip_biometric_device_id_push=True).create({
                'name': self.name or f"Employee {uid_clean}",
                'device_id_num': uid_clean,
            })

        # Update all device user records sharing this user_id
        matching_users = self.search([('user_id', '=', uid_clean)])
        matching_users.write({'employee_id': emp.id})

        return {
            'type': 'ir.actions.act_window',
            'name': _('Employee'),
            'res_model': 'hr.employee',
            'res_id': emp.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_push_to_device(self):
        """Push/update this user record to its biometric device"""
        for rec in self:
            device = rec.device_id
            try:
                zk = ZK(device.device_ip, port=device.port_number, timeout=15,
                        password=0, force_udp=False, ommit_ping=False)
            except NameError:
                raise UserError(_("Pyzk module not found. Please install it with 'pip3 install pyzk'."))

            conn = None
            try:
                conn = device.device_connect(zk)
                if not conn:
                    raise UserError(_("Unable to connect to device %s (%s).") % (device.name, device.device_ip))

                conn.disable_device()
                existing_users = conn.get_users() or []
                uid_clean = str(rec.user_id).strip()
                matched_user = next((u for u in existing_users if str(u.user_id).strip() == uid_clean), None)
                if not matched_user and rec.uid:
                    matched_user = next((u for u in existing_users if getattr(u, 'uid', 0) == rec.uid), None)

                if matched_user:
                    target_uid = matched_user.uid
                    pwd = matched_user.password or ''
                    grp = matched_user.group_id or rec.group_id or ''
                    card_val = int(rec.card) if (rec.card and rec.card.isdigit()) else int(getattr(matched_user, 'card', 0) or 0)
                else:
                    max_uid = max([u.uid for u in existing_users if getattr(u, 'uid', 0)], default=0)
                    target_uid = rec.uid if (rec.uid and rec.uid > 0) else (max_uid + 1)
                    pwd = ''
                    grp = rec.group_id or ''
                    card_val = int(rec.card) if (rec.card and rec.card.isdigit()) else 0

                priv_int = int(rec.privilege) if (rec.privilege and rec.privilege.isdigit()) else const.USER_DEFAULT
                safe_name = (rec.name or uid_clean)[:24]

                conn.set_user(
                    uid=target_uid,
                    name=safe_name,
                    privilege=priv_int,
                    password=pwd,
                    group_id=str(grp),
                    user_id=uid_clean,
                    card=card_val,
                )
                conn.refresh_data()

                rec.write({
                    'uid': target_uid,
                    'name': safe_name,
                    'last_sync_date': fields.Datetime.now(),
                })
            except Exception as err:
                raise ValidationError(_("Error updating user on %s: %s") % (device.name, err))
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

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('User Updated on Device'),
                'message': _('Successfully pushed user details to the biometric device.'),
                'type': 'success',
                'sticky': False,
            }
        }

    def action_delete_from_device(self):
        """Delete the user from the physical ZKTeco device and remove the record in Odoo"""
        for rec in self:
            device = rec.device_id
            try:
                zk = ZK(device.device_ip, port=device.port_number, timeout=15,
                        password=0, force_udp=False, ommit_ping=False)
            except NameError:
                raise UserError(_("Pyzk module not found. Please install it with 'pip3 install pyzk'."))

            conn = None
            try:
                conn = device.device_connect(zk)
                if not conn:
                    raise UserError(_("Unable to connect to device %s (%s).") % (device.name, device.device_ip))

                conn.disable_device()
                if rec.user_id:
                    conn.delete_user(user_id=str(rec.user_id).strip())
                elif rec.uid:
                    conn.delete_user(uid=rec.uid)
                conn.refresh_data()
            except Exception as err:
                raise ValidationError(_("Could not delete user from %s: %s") % (device.name, err))
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

        self.unlink()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('User Deleted'),
                'message': _('User removed from biometric device and Odoo Device User List.'),
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.client', 'tag': 'reload'},
            }
        }
