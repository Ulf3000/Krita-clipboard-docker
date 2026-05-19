# SPDX-License-Identifier: GPL-3.0-or-later
"""
bcclipboarddocker.py  –  Docker UI for the clipboard manager.
Adapted from BuliCommander clipboard panel.
"""

from PyQt5.Qt import (
    QWidget, QVBoxLayout, QHBoxLayout, QToolBar, QAction, QActionGroup,
    QListView, QAbstractItemView, QStyledItemDelegate,
    QLabel, QSizePolicy, QFrame, QSplitter,
    QStyleOptionViewItem, QPainter, QRect, QSize, QColor, QPen, QFont,
    QApplication, QMenu, QCursor, QScrollBar, QIcon,
    QAbstractListModel, QModelIndex, QVariant,
    Qt, pyqtSignal,
)
from PyQt5.QtGui import QPixmap, QFontMetrics, QPalette

from .bcclipboard import BCClipboardManager, BCClipboardItem, BCClipboardItemType


# ---------------------------------------------------------------------------
# Colour helpers (BC-style)
# ---------------------------------------------------------------------------
def _palette_color(role):
    return QApplication.palette().color(role)


# ---------------------------------------------------------------------------
# List model
# ---------------------------------------------------------------------------
class BCClipboardModel(QAbstractListModel):
    """Qt model that wraps a list of BCClipboardItem objects."""

    ROLE_ITEM     = Qt.UserRole + 1

    def __init__(self, manager: BCClipboardManager, parent=None):
        super().__init__(parent)
        self._manager = manager
        self._items: list[BCClipboardItem] = []

        manager.itemAdded.connect(self._onItemAdded)
        manager.itemUpdated.connect(self._onItemUpdated)
        manager.itemRemoved.connect(self._onItemRemoved)
        manager.cleared.connect(self._onCleared)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------
    def _onItemAdded(self, item: BCClipboardItem):
        self.beginInsertRows(QModelIndex(), 0, 0)
        self._items.insert(0, item)
        self.endInsertRows()

    def _onItemUpdated(self, item: BCClipboardItem):
        try:
            row = self._items.index(item)
        except ValueError:
            return
        idx = self.index(row)
        self.dataChanged.emit(idx, idx)

    def _onItemRemoved(self, item_uuid: str):
        for i, it in enumerate(self._items):
            if it.uuid == item_uuid:
                self.beginRemoveRows(QModelIndex(), i, i)
                self._items.pop(i)
                self.endRemoveRows()
                return

    def _onCleared(self):
        self.beginResetModel()
        self._items = [i for i in self._items if i.pinned]
        self.endResetModel()

    # ------------------------------------------------------------------
    # QAbstractListModel interface
    # ------------------------------------------------------------------
    def rowCount(self, parent=QModelIndex()):
        return len(self._items)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self._items):
            return QVariant()
        item = self._items[index.row()]
        if role == Qt.DisplayRole:
            return item.sourceShort()
        if role == Qt.DecorationRole:
            return item.thumbnail
        if role == self.ROLE_ITEM:
            return item
        return QVariant()

    def getItem(self, index: QModelIndex):
        if not index.isValid():
            return None
        return self._items[index.row()]


# ---------------------------------------------------------------------------
# Custom delegate for grid/list rendering
# ---------------------------------------------------------------------------
class BCClipboardDelegate(QStyledItemDelegate):
    """Renders each clipboard item with thumbnail + metadata."""

    THUMB_W    = 48
    THUMB_H    = 48
    PADDING    = 3
    TEXT_LINES = 3   # source, size, timestamp

    def sizeHint(self, option, index):
        return QSize(option.rect.width(),
                     self.THUMB_H + self.PADDING * 2)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem,
              index: QModelIndex):
        item: BCClipboardItem = index.data(BCClipboardModel.ROLE_ITEM)
        if item is None:
            return

        painter.save()
        rect: QRect = option.rect

        # --- Background ---
        # Call initStyleOption so Qt populates state flags correctly, then
        # suppress the view's own selection drawing by removing State_Selected
        # before calling the base paint — we draw the background ourselves so
        # the custom delegate fully owns the row appearance.
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        is_selected = bool(opt.state & 0x0020)  # QStyle.State_Selected

        if is_selected:
            bg = _palette_color(QPalette.Highlight)
            fg = _palette_color(QPalette.HighlightedText)
        else:
            bg = _palette_color(QPalette.Base)
            fg = _palette_color(QPalette.Text)
        painter.fillRect(rect, bg)

        p = self.PADDING

        # --- Thumbnail ---
        thumb_rect = QRect(rect.left() + p,
                           rect.top() + p,
                           self.THUMB_W, self.THUMB_H)
        if item.thumbnail is not None:
            # Draw checkerboard bg for transparent images
            painter.fillRect(thumb_rect, QColor(180, 180, 180))
            # Centre the thumb
            t = item.thumbnail
            ox = (self.THUMB_W - t.width())  // 2
            oy = (self.THUMB_H - t.height()) // 2
            painter.drawPixmap(thumb_rect.left() + ox,
                               thumb_rect.top()  + oy, t)
        else:
            # Placeholder
            painter.fillRect(thumb_rect, QColor(60, 60, 60))
            painter.setPen(QColor(120, 120, 120))
            painter.drawText(thumb_rect, Qt.AlignCenter, '…')

        # --- Text area ---
        # Reserve space on the right for the lock icon on pinned items.
        LOCK_W    = 14
        text_x    = thumb_rect.right() + p * 2
        text_w    = rect.right() - text_x - p - (LOCK_W + p if item.pinned else 0)
        line_h    = (self.THUMB_H) // self.TEXT_LINES
        small_fnt = painter.font()
        small_fnt.setPointSize(max(6, small_fnt.pointSize()))

        # Pinned items: amber text so the whole row stands out.
        if item.pinned and not is_selected:
            fg = QColor(220, 160, 0)

        painter.setPen(fg)
        painter.setFont(small_fnt)
        fm = QFontMetrics(small_fnt)

        def draw_line(row, label, value):
            y = rect.top() + p + row * line_h
            lbl_rect = QRect(text_x, y, text_w, line_h)
            txt = f'{label}: {value}'
            txt = fm.elidedText(txt, Qt.ElideRight, text_w)
            painter.drawText(lbl_rect, Qt.AlignLeft | Qt.AlignVCenter, txt)

        draw_line(0, item.typeLabel(), item.sourceShort(32))
        draw_line(1, 'Size',          item.sizeLabel())
        draw_line(2, 'Time',          item.timestampLabel())

        # --- Lock icon at right edge for pinned items ---
        if item.pinned:
            lock_fnt = painter.font()
            lock_fnt.setPointSize(max(8, lock_fnt.pointSize()))
            painter.setFont(lock_fnt)
            painter.setPen(QColor(220, 160, 0))
            lock_r = QRect(rect.right() - LOCK_W - p,
                           rect.top(),
                           LOCK_W, rect.height())
            painter.drawText(lock_r, Qt.AlignCenter, '🔒')

        # --- Border ---
        pen = QPen(QColor(80, 80, 80))
        pen.setWidth(1)
        painter.setPen(pen)
        painter.drawLine(rect.left(), rect.bottom(),
                         rect.right(), rect.bottom())

        painter.restore()


# ---------------------------------------------------------------------------
# Detail panel (shown below/beside list)
# ---------------------------------------------------------------------------
class BCClipboardDetail(QWidget):
    """Shows a larger preview + full metadata for the selected item."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_item = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self._preview = QLabel()
        self._preview.setAlignment(Qt.AlignCenter)
        self._preview.setMinimumHeight(120)
        self._preview.setStyleSheet('background:#b4b4b4; border:1px solid #909090;')
        layout.addWidget(self._preview)


    def setItem(self, item: BCClipboardItem):
        if item is None:
            self._preview.clear()
            return

        # Use full-resolution image; fall back to thumbnail if not loaded yet.
        if item.image is not None:
            src_px = QPixmap.fromImage(item.image)
        elif item.thumbnail is not None:
            src_px = item.thumbnail
        else:
            src_px = None

        self._current_item = item
        if src_px is not None:
            available_w = max(self._preview.width()  - 8, 1)
            available_h = max(self._preview.height() - 8, 1)
            self._preview.setPixmap(
                src_px.scaled(available_w, available_h,
                              Qt.KeepAspectRatio,
                              Qt.SmoothTransformation)
            )
        else:
            self._preview.setText('No preview')


    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._current_item is not None:
            self.setItem(self._current_item)


# ---------------------------------------------------------------------------
# Main docker content widget
# ---------------------------------------------------------------------------
class BCClipboardDockerWidget(QWidget):
    """
    The full clipboard panel that lives inside the Krita docker.
    Provides:
      • Toolbar  (start/stop monitoring, clear, view mode toggle)
      • Item list (grid thumbnails)
      • Detail panel (large preview + metadata)
      • Context menu for paste actions
    """

    def __init__(self, manager: BCClipboardManager, parent=None):
        super().__init__(parent)
        self._manager = manager
        self._model   = BCClipboardModel(manager)
        self._delegate = BCClipboardDelegate()

        self._buildUI()
        self._connectSignals()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _buildUI(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # --- Toolbar ---
        self._toolbar = QToolBar()
        self._toolbar.setMovable(False)
        self._toolbar.setIconSize(QSize(16, 16))

        self._actMonitor = QAction('⏺', self)
        self._actMonitor.setToolTip('Toggle clipboard monitoring')
        self._actMonitor.setCheckable(True)
        self._actMonitor.setChecked(True)

        self._actClear = QAction('🗑', self)
        self._actClear.setToolTip('Clear unpinned items')

        self._actClearAll = QAction('💥', self)
        self._actClearAll.setToolTip('Clear ALL items (including pinned)')

        self._actPaste = QAction('📋→Doc', self)
        self._actPaste.setToolTip('Paste selected item as new document')

        self._actPasteLayer = QAction('📋→Layer', self)
        self._actPasteLayer.setToolTip('Paste selected item as new layer')

        self._actPin = QAction('📌', self)
        self._actPin.setToolTip('Pin / unpin selected item')

        self._actRemove = QAction('✖', self)
        self._actRemove.setToolTip('Remove selected item')

        for act in (self._actMonitor, self._actClear, self._actClearAll,
                    None,
                    self._actPaste, self._actPasteLayer,
                    None,
                    self._actPin, self._actRemove):
            if act is None:
                self._toolbar.addSeparator()
            else:
                self._toolbar.addAction(act)

        root.addWidget(self._toolbar)

        # --- Status bar ---
        self._statusBar = QLabel('0 items')
        self._statusBar.setContentsMargins(4, 1, 4, 1)
        self._statusBar.setStyleSheet('font-size:10px; color:#888;')
        self._statusBar.setToolTip(f'History saved to: {self._manager.storageDir}')
        root.addWidget(self._statusBar)

        # --- Splitter: list + detail ---
        self._splitter = QSplitter(Qt.Vertical)

        # List view
        self._listView = QListView()
        self._listView.setModel(self._model)
        self._listView.setItemDelegate(self._delegate)
        self._listView.setSelectionMode(QAbstractItemView.SingleSelection)
        self._listView.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._listView.setContextMenuPolicy(Qt.CustomContextMenu)
        self._listView.setUniformItemSizes(True)
        self._listView.setSpacing(1)
        # Let the delegate draw the selection background; suppress the view's
        # own highlight overlay so the two don't fight each other.
        self._listView.setStyleSheet('QListView::item:selected { background: transparent; }')
        self._splitter.addWidget(self._listView)

        # Detail panel
        self._detail = BCClipboardDetail()
        self._splitter.addWidget(self._detail)
        self._splitter.setSizes([400, 180])

        root.addWidget(self._splitter)

        # --- Monitor indicator in status ---
        self._updateMonitorIndicator()

    def _connectSignals(self):
        self._actMonitor.toggled.connect(self._onMonitorToggled)
        self._actClear.triggered.connect(lambda: self._manager.clear(False))
        self._actClearAll.triggered.connect(lambda: self._manager.clear(True))
        self._actPaste.triggered.connect(self._onPasteAsDocument)
        self._actPasteLayer.triggered.connect(self._onPasteAsLayer)
        self._actPin.triggered.connect(self._onTogglePin)
        self._actRemove.triggered.connect(self._onRemoveSelected)

        self._listView.selectionModel().currentChanged.connect(self._onSelectionChanged)
        self._listView.customContextMenuRequested.connect(self._onContextMenu)
        self._listView.doubleClicked.connect(self._onDoubleClick)

        self._manager.itemAdded.connect(self._onItemCountChanged)
        self._manager.itemRemoved.connect(self._onItemCountChanged)
        self._manager.cleared.connect(self._onItemCountChanged)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _selectedItem(self) -> BCClipboardItem:
        idx = self._listView.currentIndex()
        if not idx.isValid():
            return None
        return self._model.getItem(idx)

    def _updateMonitorIndicator(self):
        if self._actMonitor.isChecked():
            self._actMonitor.setText('⏺')
            self._actMonitor.setToolTip('Monitoring ON – click to pause')
        else:
            self._actMonitor.setText('⏸')
            self._actMonitor.setToolTip('Monitoring PAUSED – click to resume')

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------
    def _onMonitorToggled(self, checked: bool):
        if checked:
            self._manager.start()
        else:
            self._manager.stop()
        self._updateMonitorIndicator()

    def _onSelectionChanged(self, current, previous):
        item = self._model.getItem(current)
        self._detail.setItem(item)

    def _onItemCountChanged(self, *args):
        n = self._manager.count()
        pinned = sum(1 for i in self._manager.items if i.pinned)
        self._statusBar.setText(
            f'{n} item{"s" if n != 1 else ""}  •  {pinned} pinned'
        )

    def _onPasteAsDocument(self):
        item = self._selectedItem()
        if item:
            self._manager.openAsNewDocument(item)

    def _onPasteAsLayer(self):
        item = self._selectedItem()
        if item:
            self._manager.pasteAsLayer(item)

    def _onTogglePin(self):
        item = self._selectedItem()
        if item:
            item.pinned = not item.pinned
            idx = self._listView.currentIndex()
            self._model.dataChanged.emit(idx, idx)
            self._onItemCountChanged()

    def _onRemoveSelected(self):
        item = self._selectedItem()
        if item:
            self._manager.removeItem(item.uuid)

    def _onDoubleClick(self, index: QModelIndex):
        """Double-click pastes as new document."""
        item = self._model.getItem(index)
        if item:
            self._manager.openAsNewDocument(item)

    def _onContextMenu(self, pos):
        item = self._selectedItem()
        menu = QMenu(self)

        if item:
            menu.addAction('Open as new document',
                           lambda: self._manager.openAsNewDocument(item))
            menu.addAction('Paste as layer',
                           lambda: self._manager.pasteAsLayer(item))
            menu.addAction('Copy back to clipboard',
                           lambda: self._manager.copyToClipboard(item))
            menu.addSeparator()
            pin_lbl = 'Unpin item' if item.pinned else 'Pin item'
            menu.addAction(pin_lbl, self._onTogglePin)
            menu.addAction('Remove item',
                           lambda: self._manager.removeItem(item.uuid))
            menu.addSeparator()

        menu.addAction('Clear unpinned',
                       lambda: self._manager.clear(False))
        menu.addAction('Clear ALL',
                       lambda: self._manager.clear(True))
        menu.exec_(self._listView.mapToGlobal(pos))