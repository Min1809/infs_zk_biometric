# -*- coding: utf-8 -*-
{
    'name': "INFS ZK Biometric Attendance Extension",
    'version': "17.0.1.0.0",
    'category': "Human Resources",
    'summary': "Dedicated Entrance & Exit biometric device support for ZKTeco and Odoo HR Attendance",
    'description': """
INFS ZK Biometric Attendance Extension
=======================================
This module extends `hr_zk_attendance` with dedicated biometric device roles:

* **Check-In Only (Entrance)**: Designed for entrance biometric devices. Allows multiple punches throughout the day (initial arrival, lunch break return, coffee break door unlock) without accidentally checking employees out.
* **Check-Out Only (Exit)**: Dedicated for exit biometric devices used exclusively at departure. Closes active attendance shifts without creating accidental check-ins.
* **Both / Toggle (Default)**: Standard alternating toggle mode for single-device setups.
    """,
    'author': "INFS",
    'website': "https://www.infs.com",
    'depends': ['hr_zk_attendance', 'hr_attendance'],
    'data': [
        'views/biometric_device_details_views.xml',
        'views/daily_attendance_views.xml',
    ],
    'installable': True,
    'auto_install': False,
    'application': False,
    'license': 'LGPL-3',
}
