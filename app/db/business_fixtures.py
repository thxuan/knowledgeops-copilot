"""Small, explicit local fixtures. No credentials or invented live observations."""
def business_fixtures(user_id):
    profiles = {
        "u_engineer_001": ("ENG-001", "Ubuntu 24.04 / GNOME", False, "模拟设备补丁待更新", 1),
        "u_employee_001": ("OFF-001", "Windows 11", True, "模拟设备符合基线", 2),
        "u_support_001": ("IT-001", "Ubuntu 24.04 / GNOME", True, "模拟设备符合基线", 2),
    }
    asset, system, compliant, reason, mfa = profiles.get(user_id, ("DEMO-001", "Linux / GNOME", False, "模拟设备补丁待更新", 1))
    return {
        "device_status": {
            "asset_id": asset, "os": system, "managed": True,
            "compliant": compliant, "reason": reason,
            "patch_status": "up_to_date" if compliant else "pending",
            "edr_status": "online", "disk_encrypted": True,
            "observed_at": "2026-10-16T09:00:00+08:00",
            "data_kind": "fixed_demo_snapshot",
        },
        "account_status": {
            "system": "OneGate", "account_enabled": True, "mfa_methods_count": mfa,
            "mfa_methods": ["验证器"] if mfa == 1 else ["验证器", "安全密钥"],
            "access": [{"resource": "公司办公服务", "access": "standard", "expires_at": None}],
            "temporary_prd_access": False, "temporary_prd_expires_at": None,
            "observed_at": "2026-10-16T09:00:00+08:00",
            "data_kind": "fixed_demo_snapshot",
        },
        "ticket_durations": {
            "triage_minutes": 12, "resolution_minutes": 28, "target_minutes": 60,
            "record_id": "demo-duration-" + user_id,
            "data_kind": "independent_duration_example",
            "note": "独立耗时演示记录，不对应当前审批创建的工单，不代表实时处理进度。",
        },
    }
