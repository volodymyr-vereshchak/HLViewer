"""aggregate_volumes line_remap semantics.

A physical line requested directly AND as a member of a requested virtual
line must report to BOTH (the virtual parent used to silently steal the
volumes, leaving the line itself with no enterprise data — night report
tabs showed GS volumes with nothing subtracted)."""

from backend.services.enterprise_volume_service import aggregate_volumes

DEVICE_A = {  # on physical line 10, also a member of virtual 208
    "serNum": 101, "mfDev": 1, "typeDev": 3, "chNum": 0,
    "line_id": 10, "branch_id": 1, "enterprise_name": "ent-a",
}
DEVICE_B = {  # on physical line 11, member of virtual 208 only
    "serNum": 102, "mfDev": 1, "typeDev": 3, "chNum": 0,
    "line_id": 11, "branch_id": 1, "enterprise_name": "ent-b",
}


def record(device, volume):
    return {
        "serNum": device["serNum"], "mfDev": device["mfDev"],
        "typeDev": device["typeDev"], "chNum": device["chNum"],
        "date": "2026-07-10", "dvstAlwrk": volume,
    }


class TestLineRemap:
    def test_member_line_reports_to_itself_and_virtual_parent(self):
        remap = {10: [10, 208], 11: [208]}
        result = aggregate_volumes(
            [record(DEVICE_A, 5.0), record(DEVICE_B, 7.0)],
            [DEVICE_A, DEVICE_B],
            "daily",
            line_remap=remap,
            none_volume_as_zero=True,
        )

        totals = {r.line_id: r.total_volume for r in result}
        # Line 10 keeps its own volume; virtual 208 aggregates both members.
        assert totals == {10: 5.0, 208: 12.0}
        # Line 11 was not requested directly → no standalone entry.
        assert 11 not in totals

    def test_unrequested_line_skipped(self):
        result = aggregate_volumes(
            [record(DEVICE_A, 5.0)],
            [DEVICE_A],
            "daily",
            line_remap={},  # nothing requested maps to A's line
        )
        assert result == []

    def test_no_remap_keeps_own_line(self):
        result = aggregate_volumes([record(DEVICE_A, 5.0)], [DEVICE_A], "daily")
        assert len(result) == 1
        assert result[0].line_id == 10
        assert result[0].total_volume == 5.0
