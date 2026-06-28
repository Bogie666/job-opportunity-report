import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

from render_manager_snapshot_email import render


def test_manager_snapshot_red_flags_are_mobile_safe_blocks():
    html = render([
        {
            'job_number': '12345',
            'job_type': 'Demand - HVAC',
            'customer': 'Test Customer',
            'appointment': 'Today',
            'snapshot': {
                'grade': 'A',
                'staffing': 'Strong sales technician recommended',
                'headline': 'Test headline',
                'signals': ['Demand call with current system issue'],
                'red_flags': [
                    'VERIFY: EVAP COIL (Trane #1) ~12 yrs - over 10-yr threshold, but records show more old coil units than the system count supports; confirm which systems are still in service before a replacement conversation.'
                ],
            },
        }
    ])
    assert 'border-radius:999px' not in html
    assert 'display:inline-block' not in html
    assert 'border-left:4px solid #c0392b' in html
    assert 'overflow-wrap:break-word' in html
    assert 'box-sizing:border-box' in html
