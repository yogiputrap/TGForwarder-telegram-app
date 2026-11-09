import html
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from telethon import Button, events, utils
from telethon.errors import MessageNotModifiedError
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault

logger = logging.getLogger(__name__)


class BotMenu:
    """Settings menu for the forwarder, operated through a Telegram bot by the admins only."""

    PAGE_SIZE = 8
    LOG_ENTRIES = 15
    DIALOG_CACHE_SECONDS = 300

    # Persistent keyboard shown under the message box; each label maps to an action
    KEYBOARD_ROWS = [
        ['📋 Daftar Rule', '➕ Tambah Rule'],
        ['🗑 Hapus Rule', '📜 Log'],
        ['✂️ Signature', '⏯ Jeda / Lanjut'],
        ['🏠 Menu'],
    ]
    KEYBOARD_ACTIONS = {
        '📋 Daftar Rule': 'list', '➕ Tambah Rule': 'add', '🗑 Hapus Rule': 'del', '📜 Log': 'log',
        '✂️ Signature': 'sig', '⏯ Jeda / Lanjut': 'pause', '🏠 Menu': 'home',
    }

    def __init__(self, bot, user, db, forwarder, admin_ids):
        self.bot = bot
        self.user = user
        self.db = db
        self.forwarder = forwarder
        self.admin_ids = admin_ids
        # Per-admin state while adding a rule: {'step': 'source'|'target', 'source': (id, title)}
        self.states = {}
        self._dialogs = []
        self._dialogs_loaded_at = 0
        self.bot.parse_mode = 'html'
        # Containers run in UTC, so show times in the configured offset (default WIB, UTC+7)
        self.tz = timezone(timedelta(hours=float(os.getenv('TZ_OFFSET_HOURS', '7'))))

    def register(self):
        self.bot.add_event_handler(self.on_id, events.NewMessage(pattern=r'^/id(@\w+)?$'))
        self.bot.add_event_handler(self.on_start, events.NewMessage(pattern=r'^/(start|menu)(@\w+)?$'))
        self.bot.add_event_handler(self.on_text, events.NewMessage(func=self._is_admin_text))
        self.bot.add_event_handler(self.on_callback, events.CallbackQuery())
        logger.info("Bot menu registered")

    async def setup_commands(self):
        """Fill the menu button (the list next to the message box) with the bot commands."""
        try:
            await self.bot(SetBotCommandsRequest(
                scope=BotCommandScopeDefault(), lang_code='',
                commands=[BotCommand('menu', 'Buka menu'), BotCommand('id', 'Lihat ID Telegram kamu')],
            ))
        except Exception as e:
            logger.warning(f"Could not set bot commands: {e}")

    def _is_admin_text(self, event):
        return (event.is_private and event.sender_id in self.admin_ids
                and not (event.raw_text or '').startswith('/'))

    # ---------- Views ----------

    def _main_menu(self):
        signature = 'ON' if self.db.get_setting('remove_signature', '1') == '1' else 'OFF'
        enabled = self.db.get_setting('enabled', '1') == '1'
        rules = len(self.db.list_rules())
        text = (f"<b>Telegram Forwarder</b>\n"
                f"Rule aktif: {rules}\n"
                f"Status: {'🟢 Berjalan' if enabled else '⏸ Dijeda'}\n"
                f"Hapus tanda \"Forwarded from\": {signature}")
        buttons = [
            [Button.inline('📋 Daftar Rule', b'm:list'), Button.inline('➕ Tambah Rule', b'm:add')],
            [Button.inline('🗑 Hapus Rule', b'm:del'), Button.inline('📜 Log', b'm:log')],
            [Button.inline(f'✂️ Hapus Signature: {signature}', b'm:sig'),
             Button.inline('▶️ Lanjutkan' if not enabled else '⏸ Jeda', b'm:pause')],
        ]
        return text, buttons

    def _keyboard(self):
        return [[Button.text(label, resize=True) for label in row] for row in self.KEYBOARD_ROWS]

    def _delete_view(self):
        rules = self.db.list_rules()
        if not rules:
            return "Belum ada rule.", self._back()
        buttons = [[Button.inline(f"🗑 {self._rule_label(r)}"[:60], f"d:{r['id']}".encode())] for r in rules]
        return "<b>Pilih rule yang dihapus</b>", buttons + self._back()

    def _log_view(self):
        buttons = [[Button.inline('🔄 Refresh', b'm:log'), Button.inline('🗑 Bersihkan', b'm:logclr')]]
        return self._log_text(), buttons + self._back()

    def _toggle(self, key):
        """Flip a 1/0 setting and return the new value as a bool."""
        new = '0' if self.db.get_setting(key, '1') == '1' else '1'
        self.db.set_setting(key, new)
        return new == '1'

    @staticmethod
    def _back():
        return [[Button.inline('⬅️ Menu', b'm:home')]]

    @staticmethod
    def _rule_label(rule):
        return f"{rule['source_title'] or rule['source_id']} → {rule['target_title'] or rule['target_id']}"

    def _rules_text(self):
        rules = self.db.list_rules()
        if not rules:
            return "Belum ada rule. Tekan ➕ Tambah Rule."
        lines = [f"{i}. {html.escape(self._rule_label(r))}\n    <code>{r['source_id']}</code> → <code>{r['target_id']}</code>"
                 for i, r in enumerate(rules, 1)]
        text = "<b>Daftar Rule</b>\n\n" + "\n".join(lines)
        return text if len(text) < 3900 else text[:3900].rsplit("\n", 1)[0] + "\n..."  # Telegram limit is 4096

    def _log_text(self):
        now = datetime.now(self.tz)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        sent, failed = self.db.activity_counts(int(midnight.timestamp()))
        lines = [f"<b>Log Aktivitas</b>  <i>(diperbarui {now:%H:%M:%S})</i>",
                 f"Hari ini: ✅ {sent} terkirim · ❌ {failed} gagal", ""]

        entries = self.db.recent_activity(self.LOG_ENTRIES)
        if not entries:
            lines.append("Belum ada aktivitas.")
        for e in entries:
            when = datetime.fromtimestamp(e['ts'], self.tz).strftime('%d/%m %H:%M:%S')
            route = f"{e['source_title'] or e['source_id']} → {e['target_title'] or e['target_id']}"
            count = f" ({e['messages']} pesan)" if e['messages'] > 1 else ""
            line = f"{when} {'✅' if e['ok'] else '❌'} {html.escape(route, quote=False)}{count}"
            if not e['ok']:
                line += f"\n    <i>{html.escape(e['detail'][:80], quote=False)}</i>"
            lines.append(line)
        return "\n".join(lines)

    async def _load_dialogs(self):
        if time.time() - self._dialogs_loaded_at > self.DIALOG_CACHE_SECONDS:
            self._dialogs = [(d.id, d.name) async for d in self.user.iter_dialogs()
                             if d.is_group or d.is_channel]
            self._dialogs_loaded_at = time.time()
        return self._dialogs

    async def _picker(self, step, page):
        """Chat picker as paginated inline buttons, for choosing a source or a target."""
        dialogs = await self._load_dialogs()
        pages = max(1, -(-len(dialogs) // self.PAGE_SIZE))
        page = min(max(page, 0), pages - 1)
        chunk = dialogs[page * self.PAGE_SIZE:(page + 1) * self.PAGE_SIZE]

        title = 'sumber' if step == 'source' else 'target'
        text = (f"<b>Pilih {title}</b> (halaman {page + 1}/{pages})\n\n"
                f"Pilih dari daftar, atau ketik ID / @username / link t.me, "
                f"atau forward satu pesan dari chat {title}.")
        buttons = [[Button.inline(name[:40] or str(peer_id), f'pk:{step}:{peer_id}'.encode())] for peer_id, name in chunk]
        nav = []
        if page > 0:
            nav.append(Button.inline('◀️', f'pg:{step}:{page - 1}'.encode()))
        if page < pages - 1:
            nav.append(Button.inline('▶️', f'pg:{step}:{page + 1}'.encode()))
        if nav:
            buttons.append(nav)
        footer = [Button.inline('❌ Batal', b'm:home')]
        if step == 'target':
            footer.insert(0, Button.inline('⬅️ Kembali', b'm:back'))
        buttons.append(footer)
        return text, buttons

    # ---------- Handlers ----------

    async def on_id(self, event):
        await event.respond(f"ID Telegram kamu: <code>{event.sender_id}</code>")

    async def on_start(self, event):
        if event.sender_id not in self.admin_ids:
            await event.respond(f"Akses ditolak. ID kamu: <code>{event.sender_id}</code>\n"
                                f"Tambahkan ke ADMIN_IDS untuk memakai bot ini.")
            return
        self.states.pop(event.sender_id, None)
        # A message carries either a reply keyboard or inline buttons, so send them separately
        await event.respond("Gunakan tombol menu di bawah ⬇️", buttons=self._keyboard())
        text, buttons = self._main_menu()
        await event.respond(text, buttons=buttons)

    async def on_callback(self, event):
        if event.sender_id not in self.admin_ids:
            await event.answer('Akses ditolak', alert=True)
            return

        data = event.data.decode()
        try:
            await self._handle_callback(event, data)
        except MessageNotModifiedError:
            await event.answer()
        except Exception as e:
            logger.error(f"Callback error ({data}): {e}")
            await event.answer(f'Error: {e}', alert=True)

    async def _handle_callback(self, event, data):
        user_id = event.sender_id
        action, _, arg = data.partition(':')

        if data == 'm:home':
            self.states.pop(user_id, None)
            text, buttons = self._main_menu()
            await event.edit(text, buttons=buttons)

        elif data == 'm:back':
            # Back from choosing the target to choosing the source
            self.states[user_id] = {'step': 'source'}
            text, buttons = await self._picker('source', 0)
            await event.edit(text, buttons=buttons)

        elif data in ('m:log', 'm:logclr'):
            if data == 'm:logclr':
                self.db.clear_activity()
            text, buttons = self._log_view()
            await event.edit(text, buttons=buttons)

        elif data == 'm:list':
            await event.edit(self._rules_text(), buttons=self._back())

        elif data == 'm:add':
            self.states[user_id] = {'step': 'source'}
            text, buttons = await self._picker('source', 0)
            await event.edit(text, buttons=buttons)

        elif data == 'm:del':
            text, buttons = self._delete_view()
            await event.edit(text, buttons=buttons)

        elif action == 'd':
            rule = self.db.get_rule(int(arg))
            if not rule:
                await event.answer('Rule tidak ditemukan', alert=True)
                return
            buttons = [[Button.inline('✅ Ya, hapus', f"dy:{rule['id']}".encode()), Button.inline('❌ Batal', b'm:del')]]
            await event.edit(f"Hapus rule ini?\n<b>{html.escape(self._rule_label(rule))}</b>", buttons=buttons)

        elif action == 'dy':
            self.db.delete_rule(int(arg))
            self.forwarder.reload()
            await event.edit("✅ Rule dihapus.", buttons=self._back())

        elif data == 'm:sig':
            self._toggle('remove_signature')
            text, buttons = self._main_menu()
            await event.edit(text, buttons=buttons)

        elif data == 'm:pause':
            self._toggle('enabled')
            text, buttons = self._main_menu()
            await event.edit(text, buttons=buttons)

        elif action in ('pg', 'pk'):
            step, _, value = arg.partition(':')
            state = self.states.get(user_id)
            if not state:
                await event.answer('Sesi habis, mulai lagi dari menu', alert=True)
                return
            if state['step'] != step:
                await event.answer('Langkah ini sudah lewat, pakai pesan terbaru', alert=True)
                return
            if action == 'pg':
                text, buttons = await self._picker(step, int(value))
                await event.edit(text, buttons=buttons)
            else:
                peer_id = int(value)
                title = next((name for pid, name in self._dialogs if pid == peer_id), str(peer_id))
                await self._chosen(event, user_id, peer_id, title, via_button=True)

        else:
            await event.answer('Menu tidak dikenal, buka /menu lagi', alert=True)

    async def on_text(self, event):
        """Typed or forwarded input while the admin is choosing a source or target."""
        user_id = event.sender_id
        action = self.KEYBOARD_ACTIONS.get((event.raw_text or '').strip())
        if action:
            try:
                await self._keyboard_action(event, user_id, action)
            except Exception as e:
                logger.error(f"Keyboard action error ({action}): {e}")
                await event.respond(f"❌ Gagal: {html.escape(str(e))}")
            return

        state = self.states.get(user_id)
        if not state:
            await event.respond("Ketik /menu untuk membuka menu.")
            return

        try:
            peer_id, title = await self._resolve(event.message)
        except Exception as e:
            await event.respond(f"❌ Chat tidak ditemukan: {html.escape(str(e))}\n"
                                f"Pastikan akun forwarder sudah bergabung di chat itu.")
            return
        try:
            await self._chosen(event, user_id, peer_id, title)
        except Exception as e:
            logger.error(f"Error adding rule: {e}")
            await event.respond(f"❌ Gagal: {html.escape(str(e))}")

    async def _keyboard_action(self, event, user_id, action):
        """Handle a tap on the persistent keyboard. Pressing one also cancels an add-rule in progress."""
        self.states.pop(user_id, None)

        if action == 'home':
            text, buttons = self._main_menu()
            await event.respond(text, buttons=buttons)
        elif action == 'list':
            await event.respond(self._rules_text(), buttons=self._back())
        elif action == 'add':
            self.states[user_id] = {'step': 'source'}
            text, buttons = await self._picker('source', 0)
            await event.respond(text, buttons=buttons)
        elif action == 'del':
            text, buttons = self._delete_view()
            await event.respond(text, buttons=buttons)
        elif action == 'log':
            text, buttons = self._log_view()
            await event.respond(text, buttons=buttons)
        elif action == 'sig':
            on = self._toggle('remove_signature')
            await event.respond(f"✂️ Hapus tanda \"Forwarded from\": <b>{'ON' if on else 'OFF'}</b>")
        elif action == 'pause':
            on = self._toggle('enabled')
            await event.respond("🟢 Forwarding berjalan" if on else "⏸ Forwarding dijeda")

    async def _resolve(self, message):
        """Turn typed text, a t.me link or a forwarded message into (peer_id, title) using the user account."""
        if message.fwd_from and message.fwd_from.from_id:
            peer = utils.get_peer_id(message.fwd_from.from_id)
        else:
            text = (message.raw_text or '').strip()
            link = re.search(r't\.me/(?:c/(\d+)|([A-Za-z0-9_]+))', text)
            if link and link.group(1):
                peer = int('-100' + link.group(1))
            elif link:
                peer = '@' + link.group(2)
            elif re.fullmatch(r'-?\d+', text):
                peer = int(text)
            elif text.startswith('@'):
                peer = text
            else:
                raise ValueError("kirim ID, @username, link t.me, atau forward pesan")
        entity = await self.user.get_entity(peer)
        return utils.get_peer_id(entity), utils.get_display_name(entity)

    async def _chosen(self, event, user_id, peer_id, title, via_button=False):
        """Advance the add-rule flow: first the source, then the target."""
        state = self.states[user_id]

        if state['step'] == 'source':
            state.update(step='target', source=(peer_id, title))
            text, buttons = await self._picker('target', 0)
            text = f"Sumber: <b>{html.escape(title)}</b>\n\n" + text
            await self._reply(event, via_button, text, buttons)
            return

        source_id, source_title = state['source']
        error = None
        if peer_id == source_id:
            error = "Sumber dan target tidak boleh sama."
        elif any(r['source_id'] == peer_id and r['target_id'] == source_id for r in self.db.list_rules()):
            error = "Rule kebalikan sudah ada, ini akan membuat pesan berputar tanpa henti."
        if error:
            text, buttons = await self._picker('target', 0)
            await self._reply(event, via_button, f"❌ {error}\n\n" + text, buttons)
            return

        added = self.db.add_rule(source_id, peer_id, source_title, title)
        self.states.pop(user_id, None)
        self.forwarder.reload()
        message = (f"✅ Rule ditambahkan:\n<b>{html.escape(source_title)} → {html.escape(title)}</b>" if added
                   else "Rule itu sudah ada.")
        await self._reply(event, via_button, message, self._back())

    @staticmethod
    async def _reply(event, via_button, text, buttons):
        """Edit the menu message after a button press, or send a new one after typed input."""
        if via_button:
            await event.edit(text, buttons=buttons)
        else:
            await event.respond(text, buttons=buttons)
