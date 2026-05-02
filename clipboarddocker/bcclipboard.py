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
import urllib.request
from pathlib import Path

from krita import Krita

from PyQt5.Qt import (
    QObject, QPixmap, QImage, QByteArray, QBuffer, QIODevice,
    QApplication, QClipboard, QMimeData,
    QThread, pyqtSignal, QTimer, QMutex, QMutexLocker,
    QStandardPaths,
)


# ---------------------------------------------------------------------------
# Storage path
# ---------------------------------------------------------------------------
def _data_dir() -> Path:
    """Return (and create) the plugin's persistent storage directory."""
    # QStandardPaths gives us the right folder cross-platform
    base = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    d = Path(base) / 'clipboarddocker'
    d.mkdir(parents=True, exist_ok=True)
    (d / 'images').mkdir(exist_ok=True)
    return d


HISTORY_FILE = 'history.json'
IMAGES_DIR   = 'images'
MAX_IMAGE_MB  = 50   # refuse to save images larger than this


# ---------------------------------------------------------------------------
# Item types
# ---------------------------------------------------------------------------
class BCClipboardItemType:
    UNKNOWN  = 'unknown'
    IMAGE    = 'image'      # raw image data in clipboard
    URL      = 'url'        # URL string (image will be downloaded)
    FILE     = 'file'       # local file path(s)
    LAYER    = 'layer'      # copied from Krita layer (internal)


# ---------------------------------------------------------------------------
# A single clipboard entry
# ---------------------------------------------------------------------------
class BCClipboardItem(QObject):
    """One persistent entry in the clipboard history."""

    thumbnailReady = pyqtSignal(str)   # emits item uuid when thumb is available

    # Default thumbnail size
    THUMB_W = 128
    THUMB_H = 128

    def __init__(self, item_type=BCClipboardItemType.UNKNOWN,
                 source_info='', parent=None):
        super().__init__(parent)
        self._uuid       = str(uuid.uuid4())
        self._type       = item_type
        self._source     = source_info          # url / filepath / description
        self._image      = None                 # QImage, loaded lazily
        self._thumbnail  = None                 # QPixmap thumbnail
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
        self._image  = qimage
        self._size_w = qimage.width()
        self._size_h = qimage.height()
        # SHA-1 hash of raw pixel data for duplicate detection
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.WriteOnly)
        qimage.save(buf, 'PNG')
        self._hash = hashlib.sha1(ba.data()).hexdigest()
        self._buildThumbnail()
        return True

    def _buildThumbnail(self):
        if self._image is None:
            return
        px = QPixmap.fromImage(self._image)
        self._thumbnail = px.scaled(
            self.THUMB_W, self.THUMB_H,
            aspectRatioMode=1,          # Qt.KeepAspectRatio
            transformMode=1             # Qt.SmoothTransformation
        )

    def typeLabel(self):
        labels = {
            BCClipboardItemType.IMAGE:  'Image',
            BCClipboardItemType.URL:    'URL',
            BCClipboardItemType.FILE:   'File',
            BCClipboardItemType.LAYER:  'Layer',
            BCClipboardItemType.UNKNOWN:'?',
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
        """Return a JSON-serialisable dict of metadata (no image data)."""
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
        item._uuid    = d.get('uuid', item._uuid)
        item._pinned  = d.get('pinned', False)
        item._size_w  = d.get('size_w', 0)
        item._size_h  = d.get('size_h', 0)
        item._hash    = d.get('hash', '')
        try:
            item._timestamp = datetime.datetime.fromisoformat(d['timestamp'])
        except Exception:
            pass
        # Load image from disk
        if image_path.exists():
            img = QImage(str(image_path))
            if not img.isNull():
                item._image = img
                item._buildThumbnail()
        return item


# ---------------------------------------------------------------------------
# URL download worker thread
# ---------------------------------------------------------------------------
class BCClipboardURLWorker(QThread):
    """Downloads an image from a URL in the background."""
    finished = pyqtSignal(str, QImage)   # uuid, image (null if failed)
    error    = pyqtSignal(str, str)      # uuid, message

    def __init__(self, item_uuid, url, parent=None):
        super().__init__(parent)
        self._uuid = item_uuid
        self._url  = url

    def run(self):
        try:
            req = urllib.request.Request(
                self._url,
                headers={'User-Agent': 'Mozilla/5.0 KritaClipboardDocker/1.0'}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
            img = QImage()
            img.loadFromData(data)
            self.finished.emit(self._uuid, img)
        except Exception as exc:
            self.finished.emit(self._uuid, QImage())
            self.error.emit(self._uuid, str(exc))


# ---------------------------------------------------------------------------
# Clipboard manager
# ---------------------------------------------------------------------------
class BCClipboardManager(QObject):
    """
    Monitors the system clipboard and maintains a persistent history of
    copied image items.  Re-implements the core logic from BuliCommander's
    clipboard subsystem without the file-manager dependencies.
    """

    itemAdded    = pyqtSignal(BCClipboardItem)
    itemUpdated  = pyqtSignal(BCClipboardItem)
    itemRemoved  = pyqtSignal(str)              # uuid
    cleared      = pyqtSignal()

    MAX_HISTORY  = 64

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list[BCClipboardItem] = []
        self._mutex  = QMutex()
        self._active = False
        self._known_hashes: set[str] = set()

        # Persistent storage paths (resolved lazily so QApplication is ready)
        self._data_dir: Path = None

        # Poll-based watching (clipboard dataChanged is not reliable cross-platform)
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._checkClipboard)
        self._last_clipboard_text = ''
        self._last_clipboard_hash = ''

        self._download_workers: dict[str, BCClipboardURLWorker] = {}

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------
    def start(self):
        if not self._active:
            self._active = True
            self._loadHistory()          # ← load saved history first
            self._timer.start()
            QApplication.clipboard().dataChanged.connect(self._onClipboardDataChanged)

    def stop(self):
        self._active = False
        self._timer.stop()
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
        """Write history.json + any missing image PNGs."""
        d = self._getDataDir()
        # Save images for items that don't have a file yet
        for item in self._items:
            img_path = self._imagePath(item.uuid)
            if item.image is not None and not img_path.exists():
                # Skip very large images to avoid filling disk
                sz_mb = (item.sizeW * item.sizeH * 4) / (1024 * 1024)
                if sz_mb <= MAX_IMAGE_MB:
                    item.image.save(str(img_path), 'PNG')
        # Write metadata
        meta = [i.toDict() for i in self._items]
        try:
            with open(d / HISTORY_FILE, 'w', encoding='utf-8') as f:
                json.dump(meta, f, indent=2)
        except Exception as e:
            print(f'[ClipboardDocker] Could not save history: {e}')

    def _loadHistory(self):
        """Read history.json and reconstruct items from saved PNGs."""
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
        for meta in meta_list:
            item_uuid = meta.get('uuid', '')
            if not item_uuid:
                continue
            img_path = self._imagePath(item_uuid)
            item = BCClipboardItem.fromDict(meta, img_path)
            loaded.append(item)
            if item.hash:
                self._known_hashes.add(item.hash)

        # Bulk-insert without triggering individual itemAdded signals
        self._items = loaded
        # Notify UI to rebuild
        for item in self._items:
            self.itemAdded.emit(item)

        print(f'[ClipboardDocker] Loaded {len(loaded)} items from history')

    # ------------------------------------------------------------------
    # Clipboard detection
    # ------------------------------------------------------------------
    def _onClipboardDataChanged(self):
        self._checkClipboard()

    def _checkClipboard(self):
        cb   = QApplication.clipboard()
        mime = cb.mimeData()
        if mime is None:
            return

        # --- Image directly in clipboard ---
        if mime.hasImage():
            img = cb.image()
            if img and not img.isNull():
                ba = QByteArray()
                buf = QBuffer(ba)
                buf.open(QIODevice.WriteOnly)
                img.save(buf, 'PNG')
                h = hashlib.sha1(ba.data()).hexdigest()
                if h != self._last_clipboard_hash:
                    self._last_clipboard_hash = h
                    item = BCClipboardItem(BCClipboardItemType.IMAGE, 'Clipboard image')
                    item.setImage(img)
                    self._addItem(item)
            return

        # --- URL(s) in clipboard text ---
        if mime.hasText():
            text = mime.text().strip()
            if text == self._last_clipboard_text:
                return
            self._last_clipboard_text = text

            # Check if it looks like an image URL
            url_pat = re.compile(
                r'^https?://\S+\.(?:png|jpe?g|gif|webp|bmp|tiff?|svg)(\?.*)?$',
                re.IGNORECASE
            )
            if url_pat.match(text):
                item = BCClipboardItem(BCClipboardItemType.URL, text)
                self._addItem(item)
                self._startURLDownload(item)
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
    # URL download
    # ------------------------------------------------------------------
    def _startURLDownload(self, item: BCClipboardItem):
        worker = BCClipboardURLWorker(item.uuid, item.source, self)
        worker.finished.connect(self._onURLDownloadFinished)
        self._download_workers[item.uuid] = worker
        worker.start()

    def _onURLDownloadFinished(self, item_uuid: str, img: QImage):
        item = self.getItem(item_uuid)
        if item is not None:
            if not img.isNull():
                item.setImage(img)
            self.itemUpdated.emit(item)
            self._saveHistory()
        if item_uuid in self._download_workers:
            del self._download_workers[item_uuid]

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
            # Trim history (keep pinned items)
            unpinned = [i for i in self._items if not i.pinned]
            while len(self._items) > self.MAX_HISTORY and unpinned:
                oldest = unpinned.pop()
                self._items.remove(oldest)
                if oldest.hash:
                    self._known_hashes.discard(oldest.hash)
                # Delete the saved PNG for the evicted item
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
        # Delete PNG from disk
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
        # Delete PNGs for removed items
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
        """Return the path where history is saved (for display in UI)."""
        return str(_data_dir())

    def saveHistory(self):
        """Public alias – call to force a save."""
        self._saveHistory()

    # ------------------------------------------------------------------
    # Actions on items
    # ------------------------------------------------------------------
    def copyToClipboard(self, item: BCClipboardItem):
        """Put the item's image back onto the system clipboard."""
        if item.image is None:
            return
        QApplication.clipboard().setImage(item.image)

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
        layer.setPixelData(
            item.image.convertToFormat(QImage.Format_ARGB32).bits().asstring(
                item.sizeW * item.sizeH * 4
            ),
            0, 0, item.sizeW, item.sizeH
        )
        doc.refreshProjection()

    def openAsReferenceImage(self, item: BCClipboardItem):
        """Add item image as a reference image in the active Krita document."""
        if item.image is None:
            return
        app = Krita.instance()
        win = app.activeWindow()
        if win is None:
            return
        view = win.activeView()
        if view is None:
            return
        # Save temp file and add as reference
        import tempfile
        tmp = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
        item.image.save(tmp.name, 'PNG')
        tmp.close()
        view.document().addDotPoint(tmp.name)   # Krita scripting API

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
        layer.setPixelData(
            item.image.convertToFormat(QImage.Format_ARGB32).bits().asstring(
                item.sizeW * item.sizeH * 4
            ),
            0, 0, item.sizeW, item.sizeH
        )
        doc.setActiveNode(layer)
        doc.refreshProjection()
