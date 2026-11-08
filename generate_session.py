"""Create a SESSION_STRING for the user account, to use on a server without interactive login.

Run it on your own machine, then put the printed string in the SESSION_STRING environment variable.
This always makes a fresh login, so the server session is independent of your local one.
"""
import os
from dotenv import load_dotenv
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

load_dotenv()

with TelegramClient(StringSession(), int(os.environ['API_ID']), os.environ['API_HASH']) as client:
    print("\nSESSION_STRING (keep it secret, it is full access to your account):\n")
    print(client.session.save())
