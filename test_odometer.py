import pytest
from PySide6.QtCore import Qt, QAbstractAnimation
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QTableWidget, QTableWidgetItem

import odometer as od


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def enable_motion(monkeypatch):
    monkeypatch.setattr(od, "motion_enabled", lambda: True)


def test_wheels_align_from_right_across_thousand_boundary_and_preserve_formatting():
    changes = od.digit_transitions("99,999 Token", "100,000 Token")
    assert set(changes) == {0, 1, 2, 4, 5, 6}
    for index, (start, direction, steps) in changes.items():
        assert str((start + direction * steps) % 10) == "100,000 Token"[index]
    assert all(direction == -1 for _, direction, _ in od.digit_transitions("100%", "87%").values())


def test_countdown_rolls_but_absolute_date_and_clock_do_not():
    old = "6 天 19 小時後重置 · 09/21 09:14"
    new = "6 天 18 小時後重置 · 09/22 10:15"
    changes = od.digit_transitions(old, new, "重置")
    assert changes and all(i < new.index("重置") for i in changes)
    assert not od.digit_transitions("13:45 更新", "14:56 更新")


def test_label_target_is_exact_during_animation_and_unchanged_value_does_not_restart(qapp):
    label = od.OdometerLabel("99,999 Token")
    label.resize(260, 50)
    label.show()
    label.setText("100,000 Token")
    assert label.text() == "100,000 Token"
    assert label.roller.animation.state() == QAbstractAnimation.State.Running
    label.roller.animation.setCurrentTime(150)
    current = label.roller.animation.currentTime()
    label.setText("100,000 Token")
    assert label.roller.animation.currentTime() == current
    geometry = label.geometry()
    label.grab()  # exercises native glyph clipping during an active frame
    label.roller.animation.setCurrentTime(od.DURATION_MS)
    assert label.roller.progress == 1
    assert label.geometry() == geometry
    label.close()


def test_retarget_uses_current_wheels_and_hiding_stops_animation(qapp):
    label = od.OdometerLabel("10")
    label.show()
    label.setText("19")
    label.roller.animation.setCurrentTime(110)
    visible = label.roller.displayed_text()
    label.setText("25")
    assert label.roller.previous == visible
    assert label.text() == "25"
    label.hide()
    assert label.roller.progress == 1
    assert label.roller.animation.state() == QAbstractAnimation.State.Stopped


def test_loading_and_missing_values_never_replace_accessible_text_with_zero(qapp):
    label = od.OdometerLabel("— Token")
    label.show()
    label.setText("120 Token")
    assert label.roller.progress == 1  # first measurement has no fabricated baseline
    label.setText("讀取中…")
    assert label.text() == "讀取中…"
    label.setText("240 Token")
    assert label.text() == "240 Token" and label.roller.previous == "120 Token"
    label.setText("— Token")
    assert label.text() == "— Token" and label.roller.progress == 1
    label.close()


def test_reduced_motion_and_hidden_widgets_update_without_animation(qapp, monkeypatch):
    label = od.OdometerLabel("10")
    label.setText("20")
    assert label.roller.progress == 1
    label.show()
    monkeypatch.setattr(od, "motion_enabled", lambda: False)
    label.setText("30")
    assert label.roller.progress == 1 and label.text() == "30"
    label.close()


def test_table_cache_follows_model_identity_and_is_bounded_to_current_rows(qapp):
    table = QTableWidget(2, 1)
    delegate = od.OdometerItemDelegate(table)
    table.setItemDelegate(delegate)
    table.show()
    def rows(values):
        table.setRowCount(len(values))
        for index, (key, value) in enumerate(values):
            item = QTableWidgetItem(value)
            item.setData(od.KEY_ROLE, key)
            item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            table.setItem(index, 0, item)
        delegate.animate_items()
    rows([("luna-low", "100"), ("sol-high", "200")])
    rows([("sol-high", "220"), ("luna-low", "100")])
    assert table.item(0, 0).data(od.OLD_ROLE) == "200"
    assert table.item(1, 0).data(od.OLD_ROLE) == "100"
    assert delegate.animation.state() == QAbstractAnimation.State.Running
    delegate.animation.setCurrentTime(160)
    table.grab()
    rows([("astra-medium", "1,234")])
    assert delegate.progress == 1
    assert delegate.values == {"astra-medium": "1,234"}
    table.close()


def test_identical_table_refresh_keeps_animation_clock_and_old_digits(qapp):
    table = QTableWidget(1, 1)
    delegate = od.OdometerItemDelegate(table)
    table.setItemDelegate(delegate)
    table.show()
    for value in ("10", "19"):
        item = QTableWidgetItem(value)
        item.setData(od.KEY_ROLE, "same-response")
        table.setItem(0, 0, item)
        delegate.animate_items()
    delegate.animation.setCurrentTime(150)
    same = QTableWidgetItem("19")
    same.setData(od.KEY_ROLE, "same-response")
    table.setItem(0, 0, same)
    delegate.animate_items()
    assert delegate.animation.currentTime() == 150
    assert same.data(od.OLD_ROLE) == "10"
    table.hide()
    assert delegate.animation.state() == QAbstractAnimation.State.Stopped


def test_ring_initial_placeholder_and_animated_target(qapp):
    import app
    ring = app.UsageRing()
    assert ring.roller.text == "尚無資料"
    ring.set_remaining(48)
    ring.show()
    ring.set_remaining(87)
    assert ring._remaining == 87 and ring.roller.text == "87%"
    assert ring.roller.animation.state() == QAbstractAnimation.State.Running
    ring.roller.animation.setCurrentTime(140)
    ring.grab()
    ring.close()


def test_button_count_animates_without_changing_action_text(qapp):
    button = od.OdometerButton("更多指令（11）")
    button.show()
    button.setText("更多指令（12）")
    assert button.text() == "更多指令（12）"
    button.roller.animation.setCurrentTime(120)
    button.grab()
    button.close()
