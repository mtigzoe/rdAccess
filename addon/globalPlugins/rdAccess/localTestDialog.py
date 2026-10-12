"""Standard wx controls for temporary local test authorization."""

from __future__ import annotations

import wx


class LocalTestAuthorizationDialog(wx.Dialog):
	def __init__(self, parent):
		# Translators: Title of the temporary test-session authorization dialog.
		super().__init__(parent, title=_("Authorize Linux test session"))
		sizer = wx.BoxSizer(wx.VERTICAL)
		# Translators: Explains the local test-session permission and its expiry.
		message = _(
			"Authorize programs running under your Windows account to read connection status "
			"for five minutes. "
			"You can revoke authorization from NVDA Tools. "
			"A connection created by this test session will be disconnected when authorization ends. "
			"Existing connections are preserved. Keyboard injection is unavailable.",
		)
		label = wx.StaticText(self, label=message)
		label.Wrap(480)
		sizer.Add(label, flag=wx.ALL, border=12)
		# Translators: Additional permission, unchecked by default, to use saved Remote Access settings.
		self.allowConnect = wx.CheckBox(
			self,
			label=_("Allow connection using saved Remote Access relay settings"),
		)
		sizer.Add(self.allowConnect, flag=wx.ALL, border=12)
		buttons = self.CreateButtonSizer(wx.OK | wx.CANCEL)
		# Translators: Confirms temporary authorization; this is not persistent configuration.
		self.FindWindowById(wx.ID_OK).SetLabel(_("Authorize for five minutes"))
		sizer.Add(buttons, flag=wx.ALL | wx.ALIGN_RIGHT, border=12)
		self.SetSizerAndFit(sizer)
		self.allowConnect.SetFocus()
