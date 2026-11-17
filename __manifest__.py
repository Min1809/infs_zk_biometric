# -*- coding: utf-8 -*-
{
    'name': "INFS ZK Biometric Attendance Extension",
    'version': "17.0.1.1.0",
    'category': "Human Resources",
    'summary': "Dedicated Entrance & Exit biometric device support, Employee-to-Device user creation & Device User List",
    'description': """
INFS ZK Biometric Attendance Extension
=======================================
This module extends `hr_zk_attendance` with dedicated biometric device roles and user management:

* **Check-In Only (Entrance)**: Designed for entrance biometric devices. Allows multiple punches throughout the day (initial arrival, lunch break return, coffee break door unlock) without accidentally checking employees out.
* **Check-Out Only (Exit)**: Dedicated for exit biometric devices used exclusively at departure. Closes active attendance shifts without creating accidental check-ins.
* **Both / Toggle (Default)**: Standard alternating toggle mode for single-device setups.
* **Create / Sync User on Biometric Device**: Create new employees directly from Odoo onto all configured ZKTeco fingerprint devices and automatically link their `device_id_num`.
* **Device User List**: View, search, sync, link, or delete users stored on physical ZKTeco devices under Attendances -> Biometric Device -> Device User List.
    """,
    'author': "INFS",
    'website': "https://www.infs.com",
    'depends': ['hr_zk_attendance', 'hr_attendance'],
    'data': [
        'security/ir.model.access.csv',
        'views/biometric_device_user_views.xml',
        'views/biometric_device_details_views.xml',
        'views/daily_attendance_views.xml',
        'views/hr_employee_views.xml',
    ],
    'installable': True,
    'auto_install': False,
    'application': False,
    'license': 'LGPL-3',
}
