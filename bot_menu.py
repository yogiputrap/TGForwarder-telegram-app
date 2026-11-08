import html
import logging
import re
import time
from telethon import Button, events, utils
from telethon.errors import MessageNotModifiedError

logger = logging.getLogger(__name__)


class BotMenu:
    """Settings menu for the forwarder, operated through a Telegram bot by the admins only."""

    PAGE_SIZE = 8
    DIALOG_CACHE_SECONDS = 300

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

    def register(self):
        self.bot.add_event_handler(self.on_id, events.NewMessage(pattern=r'^/id(@\w+)?$'))
        self.bot.add_event_handler(self.on_start, events.NewMessage(pattern=r'^/(start|menu)(@\w+)?$'))
        self.bot.add_event_handler(self.on_text, events.NewMessage(func=self._is_admin_text))
        self.bot.add_event_handler(self.on_callback, events.CallbackQuery())
        logger.info("Bot menu registered")

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
            [Button.inline('🗑 Hapus Rule', b'm:del')],
            [Button.inline(f'✂️ Hapus Signature: {signature}', b'm:sig'),
             Button.inline('▶️ Lanjutkan' if not enabled else '⏸ Jeda', b'm:pause')],
        ]
        return text, buttons

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

        elif data == 'm:list':
            await event.edit(self._rules_text(), buttons=self._back())

        elif data == 'm:add':
            self.states[user_id] = {'step': 'source'}
            text, buttons = await self._picker('source', 0)
            await event.edit(text, buttons=buttons)

        elif data == 'm:del':
            rules = self.db.list_rules()
            if not rules:
                await event.edit("Belum ada rule.", buttons=self._back())
                return
            buttons = [[Button.inline(f"🗑 {self._rule_label(r)}"[:60], f"d:{r['id']}".encode())] for r in rules]
            await event.edit("<b>Pilih rule yang dihapus</b>", buttons=buttons + self._back())

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
            current = self.db.get_setting('remove_signature', '1')
            self.db.set_setting('remove_signature', '0' if current == '1' else '1')
            text, buttons = self._main_menu()
            await event.edit(text, buttons=buttons)

        elif data == 'm:pause':
            current = self.db.get_setting('enabled', '1')
            self.db.set_setting('enabled', '0' if current == '1' else '1')
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
