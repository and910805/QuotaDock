"""Native Qt digit rollers. Stored text and accessible values always remain exact."""
from __future__ import annotations

import ctypes
import os
import re
from decimal import Decimal, InvalidOperation

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QVariantAnimation, QEasingCurve, QEvent
from PySide6.QtGui import QPainter, QTextLayout, QTextOption, QPalette
from PySide6.QtWidgets import (QApplication, QLabel, QPushButton, QStyle,
    QStyleOption, QStyleOptionButton, QStyleOptionViewItem, QStyledItemDelegate)

NUMBER = re.compile(r"[0-9]+(?:[,.][0-9]+)*")
TIME = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
KEY_ROLE = int(Qt.ItemDataRole.UserRole) + 41
OLD_ROLE = KEY_ROLE + 1
DURATION_MS = 480


def motion_enabled():
    if os.name == "nt":
        enabled = ctypes.c_int(1)
        if ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(enabled), 0):
            return bool(enabled.value)
    return True


def digit_transitions(old, new, exclude_after=None):
    """Match numeric fields, then align their digit wheels from the right."""
    old_fields, new_fields = list(NUMBER.finditer(old)), list(NUMBER.finditer(new))
    result = {}
    excluded = {i for match in TIME.finditer(new) for i in range(*match.span())}
    end = new.find(exclude_after) if exclude_after else -1
    for before, after in zip(old_fields, new_fields):
        previous = [c for c in before.group() if c.isdigit()]
        positions = [i for i in range(*after.span()) if new[i].isdigit()]
        previous = ["0"] * max(0, len(positions) - len(previous)) + previous
        previous = previous[-len(positions):]
        try:
            direction = 1 if Decimal(after.group().replace(",", "")) >= Decimal(before.group().replace(",", "")) else -1
        except (ValueError, InvalidOperation):
            direction = 1
        for index, start in zip(positions, previous):
            if index in excluded or (end >= 0 and index >= end) or start == new[index]:
                continue
            steps = ((int(new[index]) - int(start)) * direction) % 10
            result[index] = (int(start), direction, steps)
    return result


def draw_rolling_text(painter, rect, text, previous, progress, alignment, *, wrap=False, exclude_after=None):
    transitions = digit_transitions(previous, text, exclude_after) if progress < 1 else {}
    if not transitions:
        flags = int(alignment) | (int(Qt.TextFlag.TextWordWrap) if wrap else 0)
        painter.drawText(rect, flags, text)
        return
    layout = QTextLayout(text.replace("\n", "\u2028"), painter.font())
    option = QTextOption()
    option.setAlignment(alignment & Qt.AlignmentFlag.AlignHorizontal_Mask)
    option.setWrapMode(QTextOption.WrapMode.WordWrap if wrap else QTextOption.WrapMode.NoWrap)
    layout.setTextOption(option)
    layout.beginLayout()
    height = 0.0
    lines = []
    while True:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(rect.width())
        line.setPosition(QPointF(0, height))
        height += line.height()
        lines.append(line)
    layout.endLayout()
    top = rect.top()
    if alignment & Qt.AlignmentFlag.AlignVCenter:
        top += (rect.height() - height) / 2
    elif alignment & Qt.AlignmentFlag.AlignBottom:
        top += rect.height() - height
    metrics = painter.fontMetrics()
    painter.save()
    painter.setClipRect(rect, Qt.ClipOperation.IntersectClip)
    for line in lines:
        baseline = top + line.y() + line.ascent()
        for index in range(line.textStart(), line.textStart() + line.textLength()):
            char = text[index]
            if char == "\n":
                continue
            x = rect.left() + line.cursorToX(index)[0]
            if index not in transitions:
                painter.drawText(QPointF(x, baseline), char)
                continue
            start, direction, steps = transitions[index]
            offset = progress * steps
            tick = int(offset)
            fraction = offset - tick
            wheel_height = line.height()
            painter.save()
            painter.setClipRect(QRectF(x - 1, top + line.y(), metrics.horizontalAdvance(char) + 2, wheel_height), Qt.ClipOperation.IntersectClip)
            for delta in (0, 1):
                digit = str((start + direction * (tick + delta)) % 10)
                center = (metrics.horizontalAdvance(char) - metrics.horizontalAdvance(digit)) / 2
                y = baseline + direction * (delta - fraction) * wheel_height
                painter.drawText(QPointF(x + center, y), digit)
            painter.restore()
    painter.restore()


class DigitRoller(QObject):
    def __init__(self, owner, exclude_after=None):
        super().__init__(owner)
        self.owner, self.exclude_after = owner, exclude_after
        self.text = self.previous = self.last_number = ""
        self.progress = 1.0
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(DURATION_MS)
        self.animation.setStartValue(0.0)
        self.animation.setEndValue(1.0)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.animation.valueChanged.connect(self._frame)
        owner.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Hide:
            self.finish()
        return False

    def _frame(self, progress):
        self.progress = float(progress)
        self.owner.update()

    def displayed_text(self):
        result = list(self.text)
        for index, (start, direction, steps) in digit_transitions(self.previous, self.text, self.exclude_after).items():
            result[index] = str((start + direction * round(self.progress * steps)) % 10)
        return "".join(result)

    def set_text(self, text):
        if text == self.text:
            return
        before = self.displayed_text() if self.progress < 1 else self.text
        if not NUMBER.search(before):
            before = self.last_number
        self.animation.stop()
        self.previous, self.text = before, text
        self.progress = 1.0
        if NUMBER.search(text):
            self.last_number = text
        if before and digit_transitions(before, text, self.exclude_after) and self.owner.isVisible() and motion_enabled():
            self.progress = 0.0
            self.animation.start()
        self.owner.update()

    def finish(self):
        self.animation.stop()
        self.progress = 1.0

    def paint(self, painter, rect, alignment, wrap=False):
        draw_rolling_text(painter, QRectF(rect), self.text, self.previous, self.progress,
                          alignment, wrap=wrap, exclude_after=self.exclude_after)


class OdometerLabel(QLabel):
    def __init__(self, text="", parent=None, *, exclude_after=None):
        super().__init__(text, parent)
        self.roller = DigitRoller(self, exclude_after)
        self.roller.set_text(text)
        self.setTextFormat(Qt.TextFormat.PlainText)

    def setText(self, text):
        if hasattr(self, "roller"):
            self.roller.set_text(text)
        super().setText(text)

    def clear(self):
        self.setText("")

    def hideEvent(self, event):
        self.roller.finish()
        super().hideEvent(event)

    def paintEvent(self, event):
        if self.roller.progress >= 1:
            return super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        style = QStyleOption()
        style.initFrom(self)
        self.style().drawPrimitive(QStyle.PrimitiveElement.PE_Widget, style, painter, self)
        painter.setFont(self.font())
        painter.setPen(self.palette().color(QPalette.ColorRole.WindowText))
        rect = self.contentsRect().adjusted(self.margin(), self.margin(), -self.margin(), -self.margin())
        self.roller.paint(painter, rect, self.alignment(), self.wordWrap())


class OdometerButton(QPushButton):
    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.roller = DigitRoller(self)
        self.roller.set_text(text)

    def setText(self, text):
        if hasattr(self, "roller"):
            self.roller.set_text(text)
        super().setText(text)

    def paintEvent(self, event):
        if self.roller.progress >= 1:
            return super().paintEvent(event)
        painter = QPainter(self)
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = ""
        self.style().drawControl(QStyle.ControlElement.CE_PushButton, option, painter, self)
        painter.setFont(self.font())
        painter.setPen(self.palette().color(QPalette.ColorRole.ButtonText))
        rect = self.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, self)
        self.roller.paint(painter, rect, Qt.AlignmentFlag.AlignCenter)


class OdometerItemDelegate(QStyledItemDelegate):
    """A single animation clock per table; cache values by semantic row identity."""
    def __init__(self, table):
        super().__init__(table)
        self.table, self.values = table, {}
        self.previous_values = {}
        self.progress = 1.0
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(DURATION_MS)
        self.animation.setStartValue(0.0)
        self.animation.setEndValue(1.0)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.animation.valueChanged.connect(self._frame)
        table.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Hide:
            self.animation.stop()
            self.progress = 1.0
        return False

    def _frame(self, value):
        self.progress = float(value)
        self.table.viewport().update()

    def animate_items(self):
        values, changed = {}, False
        pending = []
        for row in range(self.table.rowCount()):
            for col in range(self.table.columnCount()):
                item = self.table.item(row, col)
                key = item.data(KEY_ROLE) if item else None
                if key is None:
                    continue
                previous = self.values.get(key, item.text())
                if self.progress < 1:
                    if previous == item.text():
                        previous = self.previous_values.get(key, previous)
                    else:
                        displayed = list(previous)
                        for index, (start, direction, steps) in digit_transitions(self.previous_values.get(key, previous), previous).items():
                            displayed[index] = str((start + direction * round(self.progress * steps)) % 10)
                        previous = "".join(displayed)
                item.setData(OLD_ROLE, previous)
                pending.append((key, previous))
                values[key] = item.text()
                changed |= bool(digit_transitions(previous, item.text()))
        if values == self.values:
            return
        self.values = values
        self.previous_values = dict(pending)
        self.animation.stop()
        self.progress = 1.0
        if changed and self.table.isVisible() and motion_enabled():
            self.progress = 0.0
            self.animation.start()

    def paint(self, painter, option, index):
        previous = index.data(OLD_ROLE)
        if not previous or self.progress >= 1:
            return super().paint(painter, option, index)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text, opt.text = opt.text, ""
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        rect = style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, opt, opt.widget)
        painter.save()
        painter.setFont(opt.font)
        role = QPalette.ColorRole.HighlightedText if opt.state & QStyle.StateFlag.State_Selected else QPalette.ColorRole.Text
        painter.setPen(opt.palette.color(role))
        draw_rolling_text(painter, QRectF(rect), text, previous, self.progress, opt.displayAlignment)
        painter.restore()
