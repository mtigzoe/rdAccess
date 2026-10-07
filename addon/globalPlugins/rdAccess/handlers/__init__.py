# RDAccess: Remote Desktop Accessibility for NVDA
# Copyright 2023 Leonard de Ruijter <alderuijter@gmail.com>
# License: GNU General Public License version 2.0 or later

from ._remoteHandler import RemoteHandler
from .remoteA11yHandler import RemoteA11yHandler, RemoteA11yObject
from .remoteAccessSemanticBraille import RemoteAccessSemanticBrailleBridge, findRemoteAccessTransport
from .remoteBrailleHandler import RemoteBrailleHandler
from .remoteSpeechHandler import RemoteSpeechHandler

__all__ = [
	"RemoteA11yHandler",
	"RemoteA11yObject",
	"RemoteAccessSemanticBrailleBridge",
	"findRemoteAccessTransport",
	"RemoteBrailleHandler",
	"RemoteHandler",
	"RemoteSpeechHandler",
]
