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

    def _validate_and_update_device_id_change(self, old_id, new_id):
        """
        Validate that `new_id` is not already in use in Odoo or on physical biometric devices,
        and if the employee was already synced to devices under `old_id`, update their ID on
        the devices in-place (preserving internal uid and fingerprint templates).
        """
        self.ensure_one()
        Employee = self.env['hr.employee'].sudo()
        DeviceUser = self.env['biometric.device.user'].sudo()
        Devices = self.env['biometric.device.details'].sudo().search([])

        # 1. Check if another Odoo employee already uses new_id
        dup_emp = Employee.with_context(active_test=False).search([
            ('id', '!=', self.id),
            ('device_id_num', '=', new_id),
        ], limit=1)
        if dup_emp:
            raise ValidationError(_(
                "Cannot change Biometric Device ID to '%s': it is already assigned to employee '%s'."
            ) % (new_id, dup_emp.name))

        # 2. Check if another biometric.device.user record in Odoo already uses new_id
        conflict_dev_user = DeviceUser.search([
            ('user_id', '=', new_id),
            ('employee_id', '!=', self.id),
        ], limit=1)
        if conflict_dev_user:
            owner_info = conflict_dev_user.employee_id.name or conflict_dev_user.name
            raise ValidationError(_(
                "Cannot change Biometric Device ID to '%s': this ID already exists on device '%s' (assigned to '%s')."
            ) % (new_id, conflict_dev_user.device_id.name, owner_info))

        # Find employee's existing device user records (linked by employee_id or old_id)
        existing_dev_records = DeviceUser.search([
            '|',
            ('employee_id', '=', self.id),
            '&', ('user_id', '=', old_id), ('old_id_marker' if False else 'user_id', '!=', False),
        ]) if old_id else DeviceUser.search([('employee_id', '=', self.id)])
        existing_rec_by_device = {r.device_id.id: r for r in existing_dev_records}

        if not Devices:
            # No physical devices configured; just update any local records
            if existing_dev_records:
                existing_dev_records.write({
                    'user_id': new_id,
                    'employee_id': self.id,
                })
            return

        # 3. Connect to physical devices to verify new_id is not taken on hardware,
        #    and if old_id exists on the device, update it in-place to new_id
        device_states = {}
        for device in Devices:
            try:
                zk = ZK(device.device_ip, port=device.port_number, timeout=15,
                        password=0, force_udp=False, ommit_ping=False)
            except NameError:
                raise UserError(_("Pyzk module not found. Please install it with 'pip3 install pyzk'."))

            conn = None
            try:
                conn = device.device_connect(zk)
                if not conn:
                    if device.id in existing_rec_by_device:
                        raise ValidationError(_(
                            "Unable to connect to biometric device '%s' (%s) to verify and update Biometric Device ID."
                        ) % (device.name, device.device_ip))
                    continue

                conn.disable_device()
                zk_users = conn.get_users() or []
                local_rec = existing_rec_by_device.get(device.id)
                old_uid_slot = local_rec.uid if (local_rec and local_rec.uid) else None

                # Find if new_id already exists on this physical device
                conflict_hw_user = next(
                    (u for u in zk_users if str(getattr(u, 'user_id', '')).strip() == new_id),
                    None
                )
                if conflict_hw_user:
                    # Only allow if it's the exact same internal uid slot of this employee
                    if not old_uid_slot or conflict_hw_user.uid != old_uid_slot:
                        raise ValidationError(_(
                            "Cannot change Biometric Device ID to '%s': User '%s' (ID: %s) already exists on physical device '%s'."
                        ) % (new_id, getattr(conflict_hw_user, 'name', new_id), new_id, device.name))

                # Find existing hardware user for old_id (or old_uid_slot)
                old_hw_user = None
                if old_id:
                    old_hw_user = next(
                        (u for u in zk_users if str(getattr(u, 'user_id', '')).strip() == old_id),
                        None
                    )
                if not old_hw_user and old_uid_slot:
                    old_hw_user = next(
                        (u for u in zk_users if getattr(u, 'uid', 0) == old_uid_slot),
                        None
                    )

                device_states[device.id] = {
                    'device': device,
                    'old_hw_user': old_hw_user,
                    'local_rec': local_rec,
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

        # 4. All checks passed! Now update the user ID in-place on devices where the user exists
        safe_name = (self.name or f"Employee {new_id}").strip()[:24]
        for dev_id, state in device_states.items():
            device = state['device']
            old_hw_user = state['old_hw_user']
            local_rec = state['local_rec']

            if not old_hw_user and not local_rec:
                continue

            conn = None
            try:
                zk = ZK(device.device_ip, port=device.port_number, timeout=15,
                        password=0, force_udp=False, ommit_ping=False)
                conn = device.device_connect(zk)
                if not conn:
                    raise ValidationError(_("Lost connection to device '%s' while updating ID.") % device.name)

                conn.disable_device()
                if old_hw_user:
                    target_uid = old_hw_user.uid
                    priv_int = int(getattr(old_hw_user, 'privilege', 0) or 0)
                    pwd = getattr(old_hw_user, 'password', '') or ''
                    grp = str(getattr(old_hw_user, 'group_id', '') or '')
                    card_val = int(getattr(old_hw_user, 'card', 0) or 0)
                    hw_name = (getattr(old_hw_user, 'name', '') or safe_name).strip()[:24]
                else:
                    zk_users = conn.get_users() or []
                    max_uid = max([getattr(u, 'uid', 0) or 0 for u in zk_users], default=0)
                    target_uid = local_rec.uid if (local_rec and local_rec.uid) else (max_uid + 1)
                    priv_int = int(local_rec.privilege) if (local_rec and local_rec.privilege and local_rec.privilege.isdigit()) else const.USER_DEFAULT
                    pwd = ''
                    grp = local_rec.group_id or ''
                    card_val = int(local_rec.card) if (local_rec and local_rec.card and local_rec.card.isdigit()) else 0
                    hw_name = safe_name

                # Calling set_user with the same internal target_uid updates user_id in-place on ZKTeco
                conn.set_user(
                    uid=target_uid,
                    name=hw_name,
                    privilege=priv_int,
                    password=pwd,
                    group_id=str(grp),
                    user_id=new_id,
                    card=card_val,
                )
                conn.refresh_data()

                if local_rec:
                    local_rec.write({
                        'uid': target_uid,
                        'user_id': new_id,
                        'name': hw_name,
                        'employee_id': self.id,
                        'last_sync_date': fields.Datetime.now(),
                    })
                else:
                    priv_str = str(priv_int) if str(priv_int) in {'0', '2', '6', '14'} else '0'
                    DeviceUser.create({
                        'device_id': device.id,
                        'uid': target_uid,
                        'user_id': new_id,
                        'name': hw_name,
                        'privilege': priv_str,
                        'card': str(card_val) if card_val else '',
                        'group_id': str(grp),
                        'employee_id': self.id,
                        'last_sync_date': fields.Datetime.now(),
                    })
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

        # Also update historical raw biometric log device_id_num for this employee if old_id was set
        if old_id:
            old_logs = self.env['zk.machine.attendance'].sudo().search([
                ('employee_id', '=', self.id),
                ('device_id_num', '=', old_id),
            ])
            if old_logs:
                old_logs.write({'device_id_num': new_id})

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.context.get('skip_biometric_device_id_push'):
            Employee = self.env['hr.employee'].sudo()
            DeviceUser = self.env['biometric.device.user'].sudo()
            for vals in vals_list:
                uid_clean = (vals.get('device_id_num') or '').strip()
                if uid_clean:
                    dup_emp = Employee.with_context(active_test=False).search([
                        ('device_id_num', '=', uid_clean)
                    ], limit=1)
                    if dup_emp:
                        raise ValidationError(_(
                            "Biometric Device ID '%s' is already assigned to employee '%s'."
                        ) % (uid_clean, dup_emp.name))
        records = super().create(vals_list)
        if not self.env.context.get('skip_biometric_device_id_push'):
            DeviceUser = self.env['biometric.device.user'].sudo()
            for emp in records:
                uid_clean = (emp.device_id_num or '').strip()
                if uid_clean:
                    matching = DeviceUser.search([('user_id', '=', uid_clean)])
                    if matching:
                        matching.write({'employee_id': emp.id})
        return records

    def write(self, vals):
        if 'device_id_num' in vals and not self.env.context.get('skip_biometric_device_id_push'):
            new_raw = vals.get('device_id_num')
            new_clean = (new_raw or '').strip() if new_raw else ''
            if new_clean:
                vals = dict(vals, device_id_num=new_clean)

            old_ids_by_emp = {emp.id: (emp.device_id_num or '').strip() for emp in self}

            for emp in self:
                old_clean = old_ids_by_emp[emp.id]
                if new_clean and new_clean != old_clean:
                    emp._validate_and_update_device_id_change(old_clean, new_clean)

            res = super().write(vals)

            DeviceUser = self.env['biometric.device.user'].sudo()
            for emp in self:
                if not new_clean:
                    old_links = DeviceUser.search([('employee_id', '=', emp.id)])
                    if old_links:
                        old_links.write({'employee_id': False})
                else:
                    matching = DeviceUser.search([('user_id', '=', new_clean)])
                    if matching:
                        matching.write({'employee_id': emp.id})
            return res

        return super().write(vals)

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
            # Ensure no other employee already uses the same Biometric Device ID
            duplicate_emp = self.env['hr.employee'].sudo().with_context(active_test=False).search([
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
            self.sudo().with_context(skip_biometric_device_id_push=True).write({'device_id_num': uid_clean})

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

    def action_sync_employee_fingerprints(self):
        """
        Fetch this employee's registered fingerprint templates from the Entrance (Check-In) device
        (or whichever device has fingerprints registered for this employee) and replicate them
        to the Exit (Check-Out) and all other configured biometric devices.
        """
        self.ensure_one()
        uid_clean = (self.device_id_num or '').strip()
        if not uid_clean:
            raise UserError(_(
                "Please set or create the Employee in the Biometric Device first so they have a Biometric Device ID."
            ))

        devices = self.env['biometric.device.details'].sudo().search([])
        if len(devices) < 2:
            raise UserError(_(
                "At least 2 Biometric Devices (e.g. Entrance and Exit) are required to sync fingerprints between devices."
            ))

        # Order devices so Check-In (Entrance) devices are checked first as the fingerprint source
        sorted_devices = devices.sorted(key=lambda d: 0 if d.device_type == 'check_in' else 1)

        source_device = None
        source_fingers = []
        source_user = None
        connection_errors = []

        for device in sorted_devices:
            conn = None
            try:
                zk = ZK(device.device_ip, port=device.port_number, timeout=15,
                        password=0, force_udp=False, ommit_ping=False)
                conn = device.device_connect(zk)
                if not conn:
                    connection_errors.append(f"{device.name}: Unable to connect")
                    continue

                conn.disable_device()
                zk_users = conn.get_users() or []
                matched_u = next(
                    (u for u in zk_users if str(getattr(u, 'user_id', '')).strip() == uid_clean),
                    None
                )
                if not matched_u:
                    continue

                # Read templates for this user (try bulk get_templates first, then fallback to get_user_template 0..9)
                user_fingers = []
                try:
                    all_tpls = conn.get_templates() or []
                    user_fingers = [t for t in all_tpls if getattr(t, 'uid', 0) == matched_u.uid and getattr(t, 'valid', 1)]
                except Exception:
                    user_fingers = []

                if not user_fingers:
                    for fid in range(10):
                        try:
                            fp = conn.get_user_template(uid=matched_u.uid, temp_id=fid)
                            if fp and getattr(fp, 'valid', 0):
                                user_fingers.append(fp)
                        except Exception:
                            continue

                if user_fingers:
                    source_device = device
                    source_fingers = user_fingers
                    source_user = matched_u
                    # Update source device's fingerprint_count in Odoo
                    src_rec = self.env['biometric.device.user'].sudo().search([
                        ('device_id', '=', device.id),
                        ('user_id', '=', uid_clean),
                    ], limit=1)
                    if src_rec:
                        src_rec.write({
                            'fingerprint_count': len(user_fingers),
                            'last_sync_date': fields.Datetime.now(),
                        })
                    break
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

        if not source_device or not source_fingers:
            raise UserError(_(
                "No registered fingerprint templates found for '%s' (Biometric ID: %s) on any connected device.\n"
                "Please register the employee's fingerprint on the Entrance device first, then click Sync Fingerprints."
            ) % (self.name, uid_clean))

        # Push user & fingerprint templates to all other devices (e.g. Exit device)
        target_devices = devices.filtered(lambda d: d.id != source_device.id)
        u_cnt, fp_cnt, repl_errors = source_device._replicate_fingerprints_to_devices(
            target_devices, only_user_ids={uid_clean}
        )
        connection_errors.extend(repl_errors)

        if fp_cnt == 0 and connection_errors:
            raise UserError(_("Failed to sync fingerprints to target device(s):\n%s") % "\n".join(connection_errors))

        msg = _("Synced %d fingerprint(s) for '%s' (ID: %s) from %s to %s.") % (
            len(source_fingers),
            self.name,
            uid_clean,
            source_device.name,
            ", ".join(target_devices.mapped('name')),
        )
        if connection_errors:
            msg += _("\nWarnings: %s") % "; ".join(connection_errors)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Fingerprints Synced'),
                'message': msg,
                'type': 'warning' if connection_errors else 'success',
                'sticky': bool(connection_errors),
                'next': {'type': 'ir.actions.client', 'tag': 'reload'},
            }
        }
