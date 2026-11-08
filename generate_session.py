"""Create a SESSION_STRING for the user account, to use on a server without interactive login.

Run it on your own machine, then put the printed string in the SESSION_STRING environment variable.
This always makes a fresh login, so the server session is independent of your local one.
"""
import asyncio
import os
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv()


async def main():
    async with TelegramClient(StringSession(), int(os.environ['API_ID']), os.environ['API_HASH']) as client:
        session_string = client.session.save()

    output_path = os.getenv('SESSION_OUTPUT')
    if output_path:
        with open(output_path, 'w') as f:
            f.write(session_string)
        print(f"\nSESSION_STRING saved to {output_path}")
    else:
        print("\nSESSION_STRING (keep it secret, it is full access to your account):\n")
        print(session_string)

asyncio.run(main())
