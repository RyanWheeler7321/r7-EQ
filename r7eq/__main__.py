from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys

from PySide6.QtCore import QCoreApplication, QLockFile, Qt, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication, QDialog

from .controller import Controller
from .storage import Store, atomic_text
from .window import MainWindow
from .visibility import NativeVisibility, Visibility, owned_dialogs


def main():
    parser = argparse.ArgumentParser(prog='R7-EQ')
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--ready-file', type=Path)
    parser.add_argument('--stop', action='store_true')
    parser.add_argument('--background', action='store_true')
    args = parser.parse_args()
    store = Store(args.data_dir)
    store.root.mkdir(parents=True, exist_ok=True)
    log = RotatingFileHandler(store.root / 'r7-eq.log', maxBytes=512 * 1024, backupCount=2, encoding='utf-8')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', handlers=[log])
    server_name = 'r7-eq-' + hashlib.sha256(str(store.root.resolve()).casefold().encode()).hexdigest()[:20]
    if args.stop:
        app = QCoreApplication(sys.argv[:1])
        socket = QLocalSocket()
        socket.connectToServer(server_name)
        if not socket.waitForConnected(1000):
            logging.info('operation=stop,status=not-running')
            return 0
        socket.write(b'close\n')
        if socket.bytesToWrite():
            socket.waitForBytesWritten(1000)
        if not socket.bytesAvailable() and not socket.waitForReadyRead(3000):
            logging.error('operation=stop,status=no-acknowledgement')
            return 2
        reply = bytes(socket.readAll()).strip()
        socket.disconnectFromServer()
        logging.info('operation=stop,status=%s', reply.decode('ascii', errors='replace'))
        return 0 if reply == b'closed' else 2
    lock = QLockFile(str(store.root / 'editor.lock'))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        if args.background:
            logging.info('operation=launch,status=already-running; no focus change')
            return 0
        app = QCoreApplication(sys.argv[:1])
        socket = QLocalSocket()
        socket.connectToServer(server_name)
        if not socket.waitForConnected(1000):
            logging.error('operation=launch,status=existing-instance-unreachable')
            return 2
        socket.write(b'show\n')
        if socket.bytesToWrite():
            socket.waitForBytesWritten(1000)
        if not socket.bytesAvailable() and not socket.waitForReadyRead(1500):
            return 2
        return 0 if bytes(socket.readAll()).strip() == b'shown' else 2
    if os.name == 'nt':
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('R7EQ')
    app = QApplication(sys.argv[:1])
    app.setApplicationName('R7-EQ')
    app.setOrganizationName('R7EQ')
    app.setFont(QFont('Segoe UI', 9))
    app.setWindowIcon(QIcon(str(Path(__file__).with_name('icon.svg'))))
    app.setQuitOnLastWindowClosed(False)
    try:
        controller = Controller(store)
    except Exception:
        logging.exception('operation=startup,status=failed; settings left unchanged')
        return 1

    class Editor(MainWindow):
        def closeEvent(self, event):
            if not getattr(self, '_stopping', False):
                visibility.request('hide')
                event.ignore()
                return
            dialogs = owned_dialogs(self)
            if dialogs or visibility.dialogs:
                controller.statusChanged.emit('error', 'Close the open dialog before quitting')
                event.ignore()
                return
            if controller.close():
                event.accept()
                QTimer.singleShot(0, app.quit)
            else:
                event.ignore()

    window = Editor(controller)
    visibility = Visibility(window, app)
    native_visibility = NativeVisibility(visibility, managed=args.data_dir is None)
    app.installNativeEventFilter(native_visibility)
    if hasattr(window, 'hideRequested'):
        window.hideRequested.connect(lambda: visibility.request('hide'))
    server = QLocalServer(app)
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    QLocalServer.removeServer(server_name)
    if not server.listen(server_name):
        logging.error('operation=startup,status=no-local-server,message=%s', server.errorString())
        controller.close()
        return 1
    sockets = set()

    def connection():
        socket = server.nextPendingConnection()
        sockets.add(socket)

        def receive():
            if socket.bytesAvailable() > 64:
                socket.disconnectFromServer()
                return
            if socket.canReadLine():
                command = bytes(socket.readLine(65)).strip()
                if command == b'close':
                    window._stopping = True
                    closed = window.close()
                    if not closed:
                        window._stopping = False
                    socket.write(b'closed\n' if closed else b'blocked\n')
                    socket.flush()
                elif command == b'show':
                    visibility.request('show')
                    window.raise_()
                    window.activateWindow()
                    socket.write(b'shown\n')
                    socket.flush()
                socket.disconnectFromServer()

        def disconnected():
            sockets.discard(socket)
            socket.deleteLater()

        socket.readyRead.connect(receive)
        socket.disconnected.connect(disconnected)
        receive()

    server.newConnection.connect(connection)
    window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
    if not args.background:
        visibility.request('show')
    controller.initialize()

    def ready():
        logging.info('operation=startup,status=ready,pid=%d,points=%d', os.getpid(), len(controller.state['points']))
        if args.ready_file:
            atomic_text(args.ready_file, json.dumps({'status': 'ready', 'pid': os.getpid(),
                                                    'data_directory': str(store.root), 'window_title': window.windowTitle(),
                                                    'hwnd': int(window.winId()), 'visible': window.isVisible()}) + '\n')

    QTimer.singleShot(0, ready)
    result = app.exec()
    controller.close()
    lock.unlock()
    return result


if __name__ == '__main__':
    raise SystemExit(main())
