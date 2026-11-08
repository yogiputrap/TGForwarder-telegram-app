import asyncio
import logging
import os
import argparse
from dotenv import load_dotenv
from telethon import TelegramClient, events, utils
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession

from db import Database
from bot_menu import BotMenu

# Load environment variables
load_dotenv()

DATA_DIR = os.getenv('DATA_DIR', '.')


def setup_logging(disable_console=False):
    """Configure logging based on console preference."""
    handlers = [logging.FileHandler(os.path.join(DATA_DIR, 'telegram_forwarder.log'))]
    if not disable_console:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=handlers
    )

logger = logging.getLogger(__name__)


class TelegramForwarder:
    """Forwards messages using the user account, driven by rules stored in the database."""

    MAX_RETRIES = 3

    def __init__(self, client, db):
        self.client = client
        self.db = db
        self.forwarding_map = {}
        self.reload()

    def reload(self):
        """Refresh the in-memory source -> targets map after the rules change."""
        self.forwarding_map = self.db.forwarding_map()
        logger.info(f"Loaded {len(self.forwarding_map)} source(s) from database")

    def register_handlers(self):
        """Register message handlers. Sources are looked up dynamically on every event."""
        @self.client.on(events.NewMessage(func=lambda e: e.chat_id in self.forwarding_map))
        async def single_handler(event):
            # Album parts are handled together by the album handler
            if event.message.grouped_id:
                return
            await self._forward(event.chat_id, [event.message])

        @self.client.on(events.Album(func=lambda e: e.chat_id in self.forwarding_map))
        async def album_handler(event):
            await self._forward(event.chat_id, event.messages)

        logger.info("Message forwarding handlers registered successfully")

    async def _forward(self, source_id, messages):
        """Forward messages to every target configured for the source."""
        if self.db.get_setting('enabled', '1') != '1':
            return

        remove_signature = self.db.get_setting('remove_signature', '1') == '1'
        logger.info(f"Received {len(messages)} message(s) in {source_id}")

        for target_id in self.forwarding_map.get(source_id, []):
            try:
                await self._deliver(source_id, target_id, messages, remove_signature)
                logger.info(f"Successfully forwarded {len(messages)} message(s) {source_id} -> {target_id}")
            except Exception as e:
                logger.error(f"Error forwarding {source_id} -> {target_id}: {e}")

    async def _deliver(self, source_id, target_id, messages, remove_signature):
        """Send to one target, waiting out Telegram rate limits."""
        for attempt in range(self.MAX_RETRIES):
            try:
                if remove_signature and self._can_resend(messages):
                    await self._send_as_new(target_id, messages)
                else:
                    await self.client.forward_messages(
                        entity=target_id,
                        messages=[m.id for m in messages],
                        from_peer=source_id
                    )
                return
            except FloodWaitError as e:
                logger.warning(f"Rate limited. Waiting {e.seconds} seconds...")
                await asyncio.sleep(e.seconds + 1)
        raise RuntimeError("Gave up after repeated rate limits")

    @staticmethod
    def _can_resend(messages):
        """Only text, photos and documents can be re-sent as new messages (polls, locations, etc. cannot)."""
        return all(not m.media or m.photo or m.document or m.web_preview for m in messages)

    async def _send_as_new(self, target_id, messages):
        """Re-send without the "Forwarded from" signature, keeping text formatting."""
        if len(messages) > 1:
            await self.client.send_file(
                target_id,
                [m.media for m in messages],
                caption=[m.raw_text or '' for m in messages],
                formatting_entities=[m.entities or [] for m in messages],
            )
            return

        message = messages[0]
        # Link previews are not files, Telegram regenerates them from the text
        media = message.media if message.photo or message.document else None
        await self.client.send_message(
            entity=target_id,
            message=message.raw_text or '',
            file=media,
            formatting_entities=message.entities,
            parse_mode=None
        )


def parse_legacy_rules():
    """Read SOURCE_ID/TARGET_ID or FORWARDING_RULES from the environment, to seed an empty database."""
    pairs = []
    source_id, target_id = os.getenv('SOURCE_ID'), os.getenv('TARGET_ID')
    if source_id and target_id:
        pairs.append((int(source_id), int(target_id)))
        return pairs

    # Format: source1:target1:target2,source2:target3
    for rule in (os.getenv('FORWARDING_RULES') or '').split(','):
        parts = rule.strip().split(':')
        if len(parts) >= 2:
            pairs.extend((int(parts[0]), int(target)) for target in parts[1:])
    return pairs


async def seed_legacy_rules(client, db):
    """Import rules from .env on first run, so existing setups keep working."""
    if db.list_rules():
        return
    for source_id, target_id in parse_legacy_rules():
        titles = []
        for peer_id in (source_id, target_id):
            try:
                titles.append(utils.get_display_name(await client.get_entity(peer_id)))
            except Exception:
                titles.append(str(peer_id))
        db.add_rule(source_id, target_id, titles[0], titles[1])
        logger.info(f"Imported rule from environment: {titles[0]} -> {titles[1]}")


async def start_health_server():
    """Answer 200 on $PORT, because some hosts treat an app that listens on no port as failed."""
    port = os.getenv('PORT')
    if not port:
        return None

    async def handle(reader, writer):
        try:
            await asyncio.wait_for(reader.read(1024), timeout=2)
        except asyncio.TimeoutError:
            pass
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, '0.0.0.0', int(port))
    logger.info(f"Health check listening on port {port}")
    return server


def load_admin_ids():
    return {int(x) for x in os.getenv('ADMIN_IDS', '').replace(' ', '').split(',') if x}


async def main():
    """Main function to run the application."""
    parser = argparse.ArgumentParser(description='Telegram Message Forwarder')
    parser.add_argument('--disable-console-log', '-q', action='store_true',
                        help='Disable console logging (only log to file)')
    args = parser.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    setup_logging(disable_console=args.disable_console_log)

    api_id = os.getenv('API_ID')
    api_hash = os.getenv('API_HASH')
    bot_token = os.getenv('BOT_TOKEN')
    session_string = os.getenv('SESSION_STRING')
    admin_ids = load_admin_ids()

    missing = [name for name, value in [('API_ID', api_id), ('API_HASH', api_hash),
                                        ('BOT_TOKEN', bot_token), ('ADMIN_IDS', admin_ids)] if not value]
    if missing:
        logger.error(f"Configuration error: missing {', '.join(missing)}. See .env.example.")
        return

    db = Database(os.path.join(DATA_DIR, 'forwarder.db'))
    if not db.get_setting('remove_signature'):
        db.set_setting('remove_signature', os.getenv('REMOVE_FORWARD_SIGNATURE', '1'))

    # The user client reads the sources and sends to the targets; the bot client is only the settings menu
    user_session = StringSession(session_string) if session_string else os.path.join(DATA_DIR, 'user_session')
    user_client = TelegramClient(user_session, int(api_id), api_hash)
    bot_client = TelegramClient(os.path.join(DATA_DIR, 'bot_session'), int(api_id), api_hash)

    health_server = await start_health_server()

    try:
        await user_client.connect()
        if not await user_client.is_user_authorized():
            logger.error("User session is not logged in. Run generate_session.py and set SESSION_STRING.")
            return
        me = await user_client.get_me()
        logger.info(f"Forwarding as {utils.get_display_name(me)}")

        # Populate the entity cache so rules can be resolved by ID
        await user_client.get_dialogs()
        await seed_legacy_rules(user_client, db)

        forwarder = TelegramForwarder(user_client, db)
        forwarder.register_handlers()

        await bot_client.start(bot_token=bot_token)
        BotMenu(bot_client, user_client, db, forwarder, admin_ids).register()
        logger.info("Telegram forwarder is now running. Press Ctrl+C to stop.")

        await asyncio.gather(user_client.run_until_disconnected(), bot_client.run_until_disconnected())
    except Exception as e:
        logger.error(f"Application error: {e}")
    finally:
        if health_server:
            health_server.close()
        await user_client.disconnect()
        await bot_client.disconnect()
        logger.info("Clients disconnected")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
