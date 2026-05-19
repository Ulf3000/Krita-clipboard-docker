# SPDX-License-Identifier: GPL-3.0-or-later
"""
bcclipboard.py  –  Clipboard item model and manager
Adapted from BuliCommander (Grum999) clipboard subsystem.
"""

import os
import re
import json
import uuid
import hashlib
import datetime
from pathlib import Path

from krita import Krita

from PyQt5.Qt import (
    QObject, QPixmap, QImage, QByteArray, QBuffer, QIODevice,
    QApplication, QClipboard, QMimeData,
    pyqtSignal, QTimer, QMutex, QMutexLocker,
    QStandardPaths,
)


# ---------------------------------------------------------------------------
# Storage path
# ---------------------------------------------------------------------------
def _data_dir() -> Path:
    """Return (and create) the plugin's persistent storage directory."""
    base = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    d = Path(base) / 'clipboarddocker'
    d.mkdir(parents=True, exist_ok=True)
    (d / 'images').mkdir(exist_ok=True)
    return d


HISTORY_FILE  = 'history.json'
IMAGES_DIR    = 'images'
MAX_IMAGE_MB  = 50   # refuse to save images larger than this


# ---------------------------------------------------------------------------
# Item types
# ---------------------------------------------------------------------------
class BCClipboardItemType:
    UNKNOWN = 'unknown'
    IMAGE   = 'image'    # raw image data in clipboard
    FILE    = 'file'     # local file path(s)
    LAYER   = 'layer'    # copied from Krita layer (internal)


# ---------------------------------------------------------------------------
# A single clipboard entry
# ---------------------------------------------------------------------------
class BCClipboardItem(QObject):
    """One persistent entry in the clipboard history."""

    thumbnailReady = pyqtSignal(str)   # emits item uuid when thumb is available

    THUMB_W = 48
    THUMB_H = 48

    def __init__(self, item_type=BCClipboardItemType.UNKNOWN,
                 source_info='', parent=None):
        super().__init__(parent)
        self._uuid       = str(uuid.uuid4())
        self._type       = item_type
        self._source     = source_info
        self._image      = None       # QImage, loaded lazily
        self._thumbnail  = None       # QPixmap thumbnail
        self._timestamp  = datetime.datetime.now()
        self._pinned     = False
        self._size_w     = 0
        self._size_h     = 0
        self._hash       = ''
        self._load_error = ''

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------
    @property
    def uuid(self):      return self._uuid
    @property
    def type(self):      return self._type
    @property
    def source(self):    return self._source
    @property
    def timestamp(self): return self._timestamp
    @property
    def pinned(self):    return self._pinned
    @pinned.setter
    def pinned(self, v): self._pinned = bool(v)
    @property
    def sizeW(self):     return self._size_w
    @property
    def sizeH(self):     return self._size_h
    @property
    def hash(self):      return self._hash
    @property
    def loadError(self): return self._load_error
    @property
    def thumbnail(self): return self._thumbnail
    @property
    def image(self):     return self._image

    # ------------------------------------------------------------------
    # Load / store
    # ------------------------------------------------------------------
    def setImage(self, qimage: QImage):
        """Store a QImage, compute hash and build thumbnail."""
        if qimage is None or qimage.isNull():
            self._load_error = 'Null image'
            return False
        # Always store a fully owned copy — the caller may have passed a QImage
        # that shares memory with an external buffer (e.g. Krita's tile pool).
        # .copy() unconditionally allocates fresh memory and detaches.
        self._image  = qimage.copy()
        self._size_w = self._image.width()
        self._size_h = self._image.height()
        # Hash raw ARGB32 pixels — no PNG encode.
        img32 = self._image.convertToFormat(QImage.Format_ARGB32)
        ptr = img32.bits()
        ptr.setsize(img32.byteCount())
        self._hash = hashlib.sha1(bytes(ptr)).hexdigest()
        self._buildThumbnail()
        return True

    def _buildThumbnail(self):
        if self._image is None:
            return
        px = QPixmap.fromImage(self._image)
        self._thumbnail = px.scaled(
            self.THUMB_W, self.THUMB_H,
            aspectRatioMode=1,   # Qt.KeepAspectRatio
            transformMode=1      # Qt.SmoothTransformation
        )

    def typeLabel(self):
        labels = {
            BCClipboardItemType.IMAGE:   'Image',
            BCClipboardItemType.FILE:    'File',
            BCClipboardItemType.LAYER:   'Layer',
            BCClipboardItemType.UNKNOWN: '?',
        }
        return labels.get(self._type, '?')

    def sizeLabel(self):
        if self._size_w and self._size_h:
            return f'{self._size_w}×{self._size_h}'
        return '–'

    def timestampLabel(self):
        return self._timestamp.strftime('%Y-%m-%d  %H:%M:%S')

    def sourceShort(self, maxlen=48):
        s = self._source
        if len(s) > maxlen:
            s = '…' + s[-(maxlen - 1):]
        return s

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------
    def toDict(self) -> dict:
        return {
            'uuid':      self._uuid,
            'type':      self._type,
            'source':    self._source,
            'timestamp': self._timestamp.isoformat(),
            'pinned':    self._pinned,
            'size_w':    self._size_w,
            'size_h':    self._size_h,
            'hash':      self._hash,
        }

    @classmethod
    def fromDict(cls, d: dict, image_path: Path):
        """Reconstruct a BCClipboardItem from saved metadata + image file."""
        item = cls(
            item_type   = d.get('type',   BCClipboardItemType.UNKNOWN),
            source_info = d.get('source', ''),
        )
        item._uuid   = d.get('uuid', item._uuid)
        item._pinned = d.get('pinned', False)
        item._size_w = d.get('size_w', 0)
        item._size_h = d.get('size_h', 0)
        item._hash   = d.get('hash', '')
        try:
            item._timestamp = datetime.datetime.fromisoformat(d['timestamp'])
        except Exception:
            pass
        if image_path.exists():
            img = QImage(str(image_path))
            if not img.isNull():
                item._image = img
                item._buildThumbnail()
        return item


# ---------------------------------------------------------------------------
# Clipboard manager
# ---------------------------------------------------------------------------
class BCClipboardManager(QObject):
    """
    Monitors the system clipboard and maintains a persistent history of
    copied image items.
    """

    itemAdded   = pyqtSignal(BCClipboardItem)
    itemUpdated = pyqtSignal(BCClipboardItem)
    itemRemoved = pyqtSignal(str)   # uuid
    cleared     = pyqtSignal()

    MAX_HISTORY = 64

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list[BCClipboardItem] = []
        self._mutex  = QMutex()
        self._active = False
        self._known_hashes: set[str] = set()

        self._data_dir: Path = None

        # Deferred read: fire once, 250 ms after dataChanged.
        # We never call cb.image() inside the dataChanged handler itself.
        # Krita emits dataChanged before its internal stroke/transaction is
        # fully committed, so reading clipboard immediately interrupts the
        # tile pool (“releasing of pooled memory has been cancelled” +
        # KisSynchronizedConnection warnings in an endless loop).
        # Single-shot delay lets Krita finish before we touch pixel data.
        self._pending_timer = QTimer(self)
        self._pending_timer.setSingleShot(True)
        self._pending_timer.setInterval(250)
        self._pending_timer.timeout.connect(self._checkClipboard)

        self._last_clipboard_hash = ''
        self._checking = False


    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------
    def start(self):
        if self._active:
            return   # FIX: strict guard — never connect the signal twice
        self._active = True
        self._loadHistory()
        # connect dataChanged exactly once; _pending_timer fires on demand
        QApplication.clipboard().dataChanged.connect(self._onClipboardDataChanged)

    def stop(self):
        if not self._active:
            return
        self._active = False
        self._pending_timer.stop()
        try:
            QApplication.clipboard().dataChanged.disconnect(self._onClipboardDataChanged)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------
    def _getDataDir(self) -> Path:
        if self._data_dir is None:
            self._data_dir = _data_dir()
        return self._data_dir

    def _imagePath(self, item_uuid: str) -> Path:
        return self._getDataDir() / IMAGES_DIR / f'{item_uuid}.png'

    def _saveHistory(self):
        d = self._getDataDir()
        for item in self._items:
            img_path = self._imagePath(item.uuid)
            if item.image is not None and not img_path.exists():
                sz_mb = (item.sizeW * item.sizeH * 4) / (1024 * 1024)
                if sz_mb <= MAX_IMAGE_MB:
                    item.image.save(str(img_path), 'PNG')
        meta = [i.toDict() for i in self._items]
        try:
            with open(d / HISTORY_FILE, 'w', encoding='utf-8') as f:
                json.dump(meta, f, indent=2)
        except Exception as e:
            print(f'[ClipboardDocker] Could not save history: {e}')

    def _loadHistory(self):
        d = self._getDataDir()
        hist_file = d / HISTORY_FILE
        if not hist_file.exists():
            return
        try:
            with open(hist_file, 'r', encoding='utf-8') as f:
                meta_list = json.load(f)
        except Exception as e:
            print(f'[ClipboardDocker] Could not load history: {e}')
            return

        loaded = []
        dirty  = False
        for meta in meta_list:
            item_uuid = meta.get('uuid', '')
            if not item_uuid:
                continue
            img_path = self._imagePath(item_uuid)
            if not img_path.exists():
                dirty = True
                continue
            item = BCClipboardItem.fromDict(meta, img_path)
            loaded.append(item)
            if item.hash:
                self._known_hashes.add(item.hash)

        self._items = loaded
        for item in self._items:
            self.itemAdded.emit(item)

        print(f'[ClipboardDocker] Loaded {len(loaded)} items from history')
        if dirty:
            self._saveHistory()
            print(f'[ClipboardDocker] Pruned missing entries and resaved history')

        # Pick up PNGs dropped into images/ manually
        known_uuids = {i.uuid for i in self._items}
        images_dir  = self._getDataDir() / IMAGES_DIR
        orphans     = []
        uuid_pat    = re.compile(
            r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
            re.IGNORECASE
        )
        for png in sorted(images_dir.glob('*.png'),
                          key=lambda p: p.stat().st_mtime, reverse=True):
            stem = png.stem
            if stem in known_uuids:
                continue
            if not uuid_pat.match(stem):
                continue
            img = QImage(str(png))
            if img.isNull():
                continue
            item = BCClipboardItem(BCClipboardItemType.IMAGE, f'Imported: {png.name}')
            item._uuid = stem
            item.setImage(img)
            mtime = png.stat().st_mtime
            item._timestamp = datetime.datetime.fromtimestamp(mtime)
            orphans.append(item)
            if item.hash:
                self._known_hashes.add(item.hash)

        if orphans:
            self._items.extend(orphans)
            for item in orphans:
                self.itemAdded.emit(item)
            self._saveHistory()
            print(f'[ClipboardDocker] Imported {len(orphans)} externally added PNG(s)')

    # ------------------------------------------------------------------
    # Clipboard detection
    # ------------------------------------------------------------------
    def _onClipboardDataChanged(self):
        # Do NOT call _checkClipboard() here directly. Krita emits dataChanged
        # before its tile transaction is done. (Re)start the single-shot timer;
        # rapid-fire copies collapse into one deferred read.
        self._pending_timer.start()

    def _checkClipboard(self):
        # Runs 250 ms after the last dataChanged — Krita is done by now.
        # Re-entrancy guard kept for safety (cb.image() can emit dataChanged
        # on some platforms, which would restart the pending timer).
        if self._checking:
            return
        self._checking = True
        try:
            self._doCheckClipboard()
        finally:
            self._checking = False

    def _doCheckClipboard(self):
        cb   = QApplication.clipboard()
        mime = cb.mimeData()
        if mime is None:
            return

        # --- Image directly in clipboard ---
        if mime.hasImage():
            img = cb.image()
            if img and not img.isNull():
                # CRITICAL: cb.image() when Krita is the clipboard owner returns
                # a QImage that shares memory with Krita's internal tile pool.
                # convertToFormat() alone is not guaranteed to detach if the
                # format already matches.  QImage.copy() always allocates fresh
                # owned memory, fully releasing Krita's tile references.
                # We must do this before touching .bits() or storing the image,
                # otherwise the tile pool can never free and emits endless
                # "releasing of pooled memory has been cancelled" + 
                # KisSynchronizedConnection warnings.
                img_owned = img.copy()
                img32 = img_owned.convertToFormat(QImage.Format_ARGB32)
                ptr = img32.bits()
                ptr.setsize(img32.byteCount())
                h = hashlib.sha1(bytes(ptr)).hexdigest()
                if h != self._last_clipboard_hash:
                    self._last_clipboard_hash = h
                    item = BCClipboardItem(BCClipboardItemType.IMAGE, 'Clipboard image')
                    item.setImage(img_owned)   # store the detached copy, not img
                    self._addItem(item)
            return


        # --- Local file paths ---
        if mime.hasUrls():
            image_exts = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp',
                          '.tiff', '.tif', '.kra', '.ora'}
            for qurl in mime.urls():
                if qurl.isLocalFile():
                    path = qurl.toLocalFile()
                    if os.path.splitext(path)[1].lower() in image_exts:
                        h = hashlib.sha1(path.encode()).hexdigest()
                        if h not in self._known_hashes:
                            item = BCClipboardItem(BCClipboardItemType.FILE, path)
                            img = QImage(path)
                            if not img.isNull():
                                item.setImage(img)
                            self._addItem(item)


    # ------------------------------------------------------------------
    # Item management
    # ------------------------------------------------------------------
    def _addItem(self, item: BCClipboardItem):
        if item.hash and item.hash in self._known_hashes:
            return   # duplicate
        with QMutexLocker(self._mutex):
            self._items.insert(0, item)
            if item.hash:
                self._known_hashes.add(item.hash)
            unpinned = [i for i in self._items if not i.pinned]
            while len(self._items) > self.MAX_HISTORY and unpinned:
                oldest = unpinned.pop()
                self._items.remove(oldest)
                if oldest.hash:
                    self._known_hashes.discard(oldest.hash)
                try:
                    self._imagePath(oldest.uuid).unlink(missing_ok=True)
                except Exception:
                    pass
                self.itemRemoved.emit(oldest.uuid)
        self.itemAdded.emit(item)
        self._saveHistory()

    def getItem(self, item_uuid: str):
        for item in self._items:
            if item.uuid == item_uuid:
                return item
        return None

    def removeItem(self, item_uuid: str):
        item = self.getItem(item_uuid)
        if item is None:
            return
        with QMutexLocker(self._mutex):
            self._items.remove(item)
            if item.hash:
                self._known_hashes.discard(item.hash)
        try:
            self._imagePath(item_uuid).unlink(missing_ok=True)
        except Exception:
            pass
        self.itemRemoved.emit(item_uuid)
        self._saveHistory()

    def clear(self, pinned_too: bool = False):
        with QMutexLocker(self._mutex):
            if pinned_too:
                removed = list(self._items)
                self._items.clear()
                self._known_hashes.clear()
            else:
                removed = [i for i in self._items if not i.pinned]
                self._items = [i for i in self._items if i.pinned]
                self._known_hashes = {i.hash for i in self._items if i.hash}
        for item in removed:
            try:
                self._imagePath(item.uuid).unlink(missing_ok=True)
            except Exception:
                pass
        self.cleared.emit()
        self._saveHistory()

    @property
    def items(self):
        return list(self._items)

    def count(self):
        return len(self._items)

    @property
    def storageDir(self) -> str:
        return str(_data_dir())

    def saveHistory(self):
        self._saveHistory()

    # ------------------------------------------------------------------
    # Actions on items
    # ------------------------------------------------------------------
    def copyToClipboard(self, item: BCClipboardItem):
        """Put the item's image back onto the system clipboard."""
        if item.image is None:
            return
        # FIX: block our own signal while we set the clipboard so we don't
        # immediately re-ingest the image we just put there.
        cb = QApplication.clipboard()
        cb.dataChanged.disconnect(self._onClipboardDataChanged)
        cb.setImage(item.image)
        self._last_clipboard_hash = item.hash   # mark as already known
        cb.dataChanged.connect(self._onClipboardDataChanged)

    def openAsNewDocument(self, item: BCClipboardItem):
        """Open item image as a new Krita document."""
        if item.image is None:
            return
        doc = Krita.instance().createDocument(
            item.sizeW, item.sizeH,
            'Clipboard image',
            'RGBA', 'U8', '', 72.0
        )
        Krita.instance().activeWindow().addView(doc)
        layer = doc.activeNode()
        img32 = item.image.convertToFormat(QImage.Format_ARGB32)
        ptr = img32.bits()
        ptr.setsize(img32.byteCount())
        layer.setPixelData(bytes(ptr), 0, 0, item.sizeW, item.sizeH)
        doc.refreshProjection()

    def openAsReferenceImage(self, item: BCClipboardItem):
        """Add item image as a reference image in the active Krita document."""
        if item.image is None:
            return
        # FIX: Krita has no addDotPoint(). The correct approach is to save a
        # temp file and use the built-in "Add Reference Image" action, or
        # simply copy to clipboard and let the user use Edit > Paste as Reference.
        # For now we just copy to clipboard with a status hint.
        self.copyToClipboard(item)
        print('[ClipboardDocker] Image copied to clipboard — '
              'use Edit > Paste as Reference Image in Krita.')

    def pasteAsLayer(self, item: BCClipboardItem):
        """Paste item image as a new paint layer in the active document."""
        if item.image is None:
            return
        app = Krita.instance()
        doc = app.activeDocument()
        if doc is None:
            return
        layer = doc.createNode('Clipboard layer', 'paintlayer')
        doc.rootNode().addChildNode(layer, None)
        img32 = item.image.convertToFormat(QImage.Format_ARGB32)
        ptr = img32.bits()
        ptr.setsize(img32.byteCount())
        layer.setPixelData(bytes(ptr), 0, 0, item.sizeW, item.sizeH)
        doc.setActiveNode(layer)
        doc.refreshProjection()